## logic.py (Core, testable)

# logic.py
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Literal, Any, Dict, List, Optional, Tuple, Set
import uuid
import json
import copy
import re
import math
import random

# -------------------------
# Exceptions
# -------------------------
class VTTError(Exception):
    pass

class OutOfBounds(VTTError):
    pass

class Occupied(VTTError):
    pass

class Blocked(VTTError):
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

# Facing name -> forward (dx, dy) in grid coords (x right, y down). Used to
# project footprint cells into a part's facing-relative region (front/back/
# left/right + corners). Diagonals are unnormalized (sign-only is what the
# projection needs). Keys match DIRECTION_ARROWS / ALLOWED_DIRECTIONS.
FACING_VECTORS: Dict[str, Tuple[int, int]] = {
    "up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0),
    "up_left": (-1, -1), "up_right": (1, -1),
    "down_left": (-1, 1), "down_right": (1, 1),
}

# Named text colors -> ANSI SGR foreground codes, for the colorized text
# renderer (Discord ```ansi blocks + ANSI terminals). The names are the
# only valid values for an entity `color` var / a team color; an unknown
# name renders uncolored. Foreground only for now.
#
# Codes are chosen to work on BOTH a real ANSI terminal AND Discord's
# ```ansi blocks. Discord supports only the 30-37 foreground set plus
# style 1 (bold) — it does NOT understand the 90-97 "bright" range — so the
# bright variants are expressed as bold + base color (`1;3X`), which renders
# bright on Discord and (bold/bright) on a terminal. Plain `gray` is bold
# black, the brightest Discord gets toward grey.
TEXT_COLORS: Dict[str, str] = {
    "black": "30", "red": "31", "green": "32", "yellow": "33",
    "blue": "34", "magenta": "35", "cyan": "36", "white": "37",
    "gray": "1;30", "grey": "1;30",
    "bright_red": "1;31", "bright_green": "1;32", "bright_yellow": "1;33",
    "bright_blue": "1;34", "bright_magenta": "1;35", "bright_cyan": "1;36",
    "bright_white": "1;37",
}

# !map resize anchors: name (+ aliases) -> (horizontal, vertical) kind.
# horizontal in {left, center, right}; vertical in {top, middle, bottom}.
# The anchor names the corner/edge where existing content stays put; the
# shift offset is derived from it (left/top = 0, right/bottom = full delta,
# center/middle = half). See Match.resize_grid.
_RESIZE_ANCHORS: Dict[str, Tuple[str, str]] = {
    "top-left": ("left", "top"), "tl": ("left", "top"),
    "topleft": ("left", "top"),
    "top": ("center", "top"), "top-center": ("center", "top"),
    "tc": ("center", "top"), "t": ("center", "top"),
    "top-right": ("right", "top"), "tr": ("right", "top"),
    "topright": ("right", "top"),
    "left": ("left", "middle"), "l": ("left", "middle"),
    "center-left": ("left", "middle"), "cl": ("left", "middle"),
    "center": ("center", "middle"), "c": ("center", "middle"),
    "middle": ("center", "middle"), "m": ("center", "middle"),
    "right": ("right", "middle"), "r": ("right", "middle"),
    "center-right": ("right", "middle"), "cr": ("right", "middle"),
    "bottom-left": ("left", "bottom"), "bl": ("left", "bottom"),
    "bottomleft": ("left", "bottom"),
    "bottom": ("center", "bottom"), "bottom-center": ("center", "bottom"),
    "bc": ("center", "bottom"), "b": ("center", "bottom"),
    "bottom-right": ("right", "bottom"), "br": ("right", "bottom"),
    "bottomright": ("right", "bottom"),
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
    # How HP carries across a polymorph/transform (!ent transform / revert,
    # transform()/revert()). 'percent' keeps the same fraction of max_hp into
    # the new form (50% of old max -> 50% of new max); 'keep' carries current
    # hp clamped to the new max; 'full' uses the target statblock's own hp.
    "transform_hp_mode": {
        "default": "percent",
        "schema": {"type": "enum", "choices": ["percent", "keep", "full"]},
        "desc": (
            "How HP carries when an entity transforms (or reverts): 'percent' "
            "(preserve the fraction of max_hp), 'keep' (carry current hp, "
            "clamped to the new max), or 'full' (use the target statblock's "
            "own hp value)."
        ),
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
    "part_name_var": {
        "default": "part_name",
        "schema": {"type": "str"},
        "desc": (
            "Var key holding a body part's ROLE name ('head', 'left_arm', "
            "'reactor') — the handle that `part(parent, name)`, the aim "
            "argument of hit_location, and destroy logic resolve against. "
            "The entity id stays unique per part; part_name is the shared "
            "role tag (every humanoid's head part can share part_name "
            "'head'). Absent = the part is only addressable by its id."
        ),
    },
    # --- Locational damage: damage_part routing knobs (per-part vars of
    #     the same name override these system defaults) ---
    "part_to_main_percent_default": {
        "default": 0,
        "schema": {"type": "number"},
        "desc": (
            "Default % of a hit on a body part that is ALSO dealt to the "
            "parent's main HP (the Helldivers-2 'damage to main' tap). "
            "Per-part override: the `to_main_percent` var (doc values: head "
            "150, chest 100, stomach 70, limb 30). 0 = a hit on the part "
            "stays on the part. Applied by damage_part, never by a raw HP "
            "write."
        ),
    },
    "part_to_main_cap_default": {
        "default": "max_hp",
        "schema": {"type": "str", "choices": ["none", "max_hp", "remaining_hp"]},
        "desc": (
            "Default for what damage feeds the to-main %. `max_hp` (HD2 "
            "default): a single hit is capped at the part's max HP before "
            "the % is taken. `remaining_hp`: capped at the part's CURRENT "
            "HP (only damage the part can absorb taps through). `none`: "
            "uncapped — the full hit taps through, so a big hit can kill the "
            "parent via overflow (HD2's uncapped parts). A 0/0 part is "
            "auto-treated as `none` (it can't absorb, so it's pure "
            "passthrough). Per-part override: the `to_main_cap` var (also "
            "accepts `absolute:<n>`)."
        ),
    },
    "part_vital_default": {
        "default": False,
        "schema": {"type": "bool"},
        "desc": (
            "Default for whether destroying a body part also kills its "
            "parent (a 'vital' weakspot — a head with its own HP, a mech "
            "reactor core). Per-part override: the `vital` var. When a "
            "vital part is destroyed by damage_part, the parent is run "
            "through the configured kill function."
        ),
    },
    "part_indestructible_default": {
        "default": False,
        "schema": {"type": "bool"},
        "desc": (
            "Default for whether a body part is incapable of dying on its "
            "own (it only goes away when the parent dies). Per-part "
            "override: the `indestructible` var. A part with max_hp <= 0 (a "
            "0/0 passthrough zone like a head/chest) is ALWAYS treated as "
            "indestructible regardless of this rule."
        ),
    },
    "segment_follow_mode": {
        "default": "trail",
        "schema": {"type": "str", "choices": ["trail", "path"]},
        "desc": (
            "How a snake's body segments follow the head. `trail` "
            "(default): each segment moves into the cell the one ahead just "
            "vacated — an always-adjacent body. `path`: the head's recent "
            "cell path is recorded and segments sit at `segment_spacing`-cell "
            "offsets back along it (allows gaps and is faithful for fast / "
            "teleporting heads). Per-snake override: the head's "
            "`__segment_follow` var."
        ),
    },
    "segment_spacing": {
        "default": 1,
        "schema": {"type": "int"},
        "desc": (
            "For `path` follow mode, how many cells of head travel separate "
            "consecutive segments (1 = adjacent). Ignored by `trail` mode "
            "(always adjacent). Per-snake override: the head's "
            "`__segment_spacing` var."
        ),
    },
    "segment_self_collision": {
        "default": False,
        "schema": {"type": "bool"},
        "desc": (
            "Whether a snake's head is BLOCKED by its own body segments. "
            "False (default): the head passes through its own body (the "
            "Terraria-Destroyer behavior) — its segments are ignored by the "
            "head's occupancy check. True: moving the head into one of its "
            "own segment cells is blocked, like classic Snake. Other "
            "entities are always blocked by segments either way. Per-snake "
            "override: the head's `__segment_self_collision` var."
        ),
    },
    "segment_death_mode": {
        "default": "none",
        "schema": {"type": "str",
                   "choices": ["none", "solid", "cascade", "split"]},
        "desc": (
            "What happens when a body SEGMENT (own-HP) is destroyed. `none`: "
            "it just becomes a dead limb (default part behavior). `solid` "
            "(Destroyer): segments are treated as a single HP pool — they "
            "should be 0/0 indestructible parts routing to main, so they "
            "never individually die; the whole snake dies with the head. "
            "`cascade`: destroying a segment also destroys every segment "
            "BEHIND it (the back of the worm is severed and removed). "
            "`split` (Eater of Worlds): the segment behind the destroyed one "
            "is PROMOTED to a new independent head, the trailing segments "
            "re-parent to it, and the new head is stamped with "
            "`segment_split_head_template` — one snake becomes two. Per-snake "
            "override: the head's (or a segment's) `__segment_death_mode` var."
        ),
    },
    "segment_split_head_template": {
        "default": {},
        "schema": {"type": "dict"},
        "desc": (
            "Vars merged onto a segment when it is PROMOTED to a head by a "
            "`split` sever (dotted paths -> values, like default_entity_vars; "
            "applied only to keys missing on the promoted segment). The home "
            "for the head's actions / passives / AI vars so each severed worm "
            "comes up fully functional. Per-snake override: the original "
            "head's `__segment_split_head_template` var (an inline dict)."
        ),
    },
    "hit_location_mode": {
        "default": "weighted",
        "schema": {"type": "str", "choices": ["weighted", "uniform"]},
        "desc": (
            "Default mode for hit_location(): `weighted` rolls a body part "
            "using each part's per-side hit_weights table (the doc's "
            "front/right/left/rear chance columns, keyed by side_hit's "
            "names front/back/left_side/right_side); `uniform` gives every "
            "part equal odds. Per-call override via the mode argument."
        ),
    },
    "hit_location_aim_weight": {
        "default": 3,
        "schema": {"type": "number"},
        "desc": (
            "Default multiplier applied to the aimed part's weight in "
            "hit_location() when an aim is given (biases toward it without "
            "guaranteeing — a 0-weight side stays 0, so you can't hit what "
            "isn't exposed). Per-call override via aim_weight; aim_bonus "
            "adds a flat amount instead/as well (for attacks that reach a "
            "normally-unexposed side, e.g. a boomerang)."
        ),
    },
    # --- AoE damage spread across body parts ---
    "part_aoe_weight_default": {
        "default": 1,
        "schema": {"type": "number"},
        "desc": (
            "Default share weight a body part gets from an area attack split "
            "by damage_spread, when the part has no `aoe_weight` var AND no "
            "`hit_weights` to sum. Per-part override: the `aoe_weight` var "
            "(else damage_spread falls back to the part's summed hit_weights, "
            "else this). Higher = catches more of an aimless blast; set a "
            "head low so explosions aren't free headshots."
        ),
    },
    "aoe_default_mode": {
        "default": "weighted",
        "schema": {"type": "str",
                   "choices": ["weighted", "uniform", "fragment", "main_only"]},
        "desc": (
            "Default mode for damage_spread(target, total): `weighted` splits "
            "total across parts proportional to aoe_weight; `uniform` splits "
            "equally; `fragment` deals aoe_fragment_count discrete weighted-"
            "random hits (shrapnel); `main_only` ignores parts and hits the "
            "main HP directly. Per-call override via the mode argument."
        ),
    },
    "aoe_fragment_count": {
        "default": 4,
        "schema": {"type": "int"},
        "desc": (
            "Default number of discrete random hits in damage_spread's "
            "`fragment` mode (each lands on a weighted-random part, dealing "
            "total/N). Per-call override via the fragments argument. A big "
            "swinging weapon is fragment with N=1-2; shrapnel is higher."
        ),
    },
    # --- Status effects on body parts ---
    "part_status_immune": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Comma-separated status names a BODY PART can't receive — "
            "apply_status on a part silently no-ops them ('fear' on an arm "
            "does nothing). Per-part override: the `__status_immune` var "
            "(replaces this list when set). Raw `!ent status` editing is a "
            "GM force-write and bypasses this."
        ),
    },
    "part_status_redirect": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Comma-separated status names that, applied to a BODY PART, are "
            "instead applied to its PARENT (for per-entity DoT in a system "
            "that doesn't track it per-limb). Per-part override: the "
            "`__status_redirect` var (replaces this list when set). Checked "
            "after immunity; raw `!ent status` editing bypasses it."
        ),
    },
    "part_custom_glyph_priority": {
        "default": True,
        "schema": {"type": "bool"},
        "desc": (
            "When True (default), a body part overlapping its parent on the "
            "map renders ABOVE the parent only if it has a CUSTOM glyph "
            "(glyph / glyphs var) — a part on the default arrow yields to the "
            "parent, so default-glyph region parts don't clobber the parent's "
            "own glyph customization. False = the parent always wins on its "
            "cells (overlapping parts never draw). Located parts on their own "
            "cells are unaffected (no overlap)."
        ),
    },
    # --- Stat modifiers (derived/effective stats) ---
    # Base stats stay plain vars and are never mutated; modifiers are data
    # records aggregated live from their SOURCES and combined on demand by
    # the apply_mods / list_mods formula primitives. A modifier record is
    # {stat, op, value, tags, not_tags, priority, condition}; value/condition
    # may be formulas (eval'd with self=owner + the call's context names).
    "modifier_sources": {
        "default": "equipped",
        "schema": {"type": "str"},
        "desc": (
            "Comma-separated vars roots walked for `modifiers` bundles when "
            "computing effective stats — so an item under one of these "
            "containers contributes its modifiers, while the same item in "
            "`inventory` does not (equip = move it under a scanned root). "
            "Status instances and a direct `entity.modifiers` slot are "
            "ALWAYS scanned on top of these. Per-entity override: the "
            "`__modifier_sources` var (replaces this list) and/or "
            "`__modifier_sources_add` var (extends it)."
        ),
    },
    "temp_hp_sources": {
        "default": "shields",
        "schema": {"type": "str"},
        "desc": (
            "Comma-separated vars roots scanned for temporary-HP / shield "
            "POOLS by absorb_damage / shield_total. Each root is a dict of "
            "NAMED pools (`shields.barrier`, `shields.ward`, ...) so multiple "
            "independent absorb layers coexist. A pool is a dict {amount, "
            "priority?, tags?, not_tags?} or a bare number (= amount). "
            "absorb_damage drains matching pools HIGHEST priority first (ties "
            "by name); a pool with `tags`/`not_tags` only absorbs hits whose "
            "tags match (a typed ward), an untagged pool absorbs anything; a "
            "pool emptied to 0 is removed. Pools are plain vars — set/refresh "
            "them with `!ent set_var` (decay is GM-composed via a status tick "
            "or round hook), per 'what X does is stored in X'. Per-entity "
            "override: the `__temp_hp_sources` var (replaces this list)."
        ),
    },
    "modifier_op_priority": {
        "default": "add:0,inc%:100,more%:200,set:300,min:400,max:500",
        "schema": {"type": "str"},
        "desc": (
            "Per-op priority OFFSETS, added to each modifier's own "
            "`priority` to get its effective priority. Modifiers fold in "
            "ascending effective priority; same-op modifiers in a tier "
            "combine naturally (add→sum, inc%→sum, more%→product, set→last, "
            "min→floor, max→cap). The defaults reproduce the classic "
            "((base+Σadd)×(1+Σinc%))×∏(1+more%) then set/clamp. Format "
            "`op:offset,op:offset` (an unlisted op offsets 0)."
        ),
    },
    "modifier_op_order": {
        "default": "add,inc%,more%,set,min,max",
        "schema": {"type": "str"},
        "desc": (
            "Tiebreak order for DIFFERENT ops that land on the same "
            "effective priority — the comma-separated op sequence applied "
            "within a tier. Ops not listed sort last (then by source order)."
        ),
    },
    "modifier_stat_caps": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Global per-stat clamps on apply_mods' FINAL value, a safety net "
            "applied even when no modifiers are present (per-ENTITY caps come "
            "from the min/max modifier ops instead). Format `stat:lo:hi` "
            "entries, comma-separated; lo and hi are each optional (blank = "
            "unbounded), e.g. `damage_dealt::500,evasion:0:95`. Empty = no "
            "caps."
        ),
    },
    # --- Entity footprint (multi-tile / large entities) ---
    # The W×H rectangle a "large" entity occupies lives in two entity
    # vars named by these rules; absent / non-positive = 1 (an ordinary
    # single-cell entity). The footprint is anchored at the entity's
    # TOP-LEFT cell (entity.x / entity.y) and extends right (+x) / down
    # (+y). Occupancy, movement, placement, rendering and (in later
    # layers) vision / distance / AoE all reason about the full
    # footprint. Set per-entity (`!ent set_var dragon footprint_w 3`),
    # via a summon template, or globally with `!defvar`.
    "footprint_width_var": {
        "default": "footprint_w",
        "schema": {"type": "str"},
        "desc": (
            "Entity-var name holding an entity's footprint WIDTH in cells "
            "(default var 'footprint_w'). Absent / <1 means width 1. Read "
            "in formulas as entity[X].footprint_w (or whatever you name it)."
        ),
    },
    "footprint_height_var": {
        "default": "footprint_h",
        "schema": {"type": "str"},
        "desc": (
            "Entity-var name holding an entity's footprint HEIGHT in cells "
            "(default var 'footprint_h'). Absent / <1 means height 1."
        ),
    },
    "aoe_origin_mode": {
        "default": "center",
        "schema": {"type": "enum", "choices": ["center", "anchor"]},
        "desc": (
            "Where the aoe_origin(eid) primitive places the origin of an "
            "area effect cast by a (possibly large) entity: 'center' = the "
            "footprint's center cell (rounded down for even sizes), 'anchor' "
            "= the top-left cell (entity.x/.y). Only affects aoe_origin(); "
            "GMs passing explicit coords are unaffected."
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
    # ---- Active Time Battle (ATB) ----
    # An optional, SYSTEM-WIDE turn model (a system is designed with it in
    # mind from the start, so it's a plain rule, not a per-match toggle).
    # When atb_enabled is on, ROUNDS ARE DISABLED: next_turn no longer cycles
    # turn_order or fires on_round_*; instead each entity accrues CHARGE into
    # a bar var (atb_charge_var) at a per-entity RATE (atb_charge_formula),
    # and the next turn goes to whoever's bar fills first. Round-coupled
    # formulas (round_number / turn_index / schedule) raise a visible error,
    # and a one-time warning fires if dormant on_round_* hooks / round status
    # ticks / round schedules exist while ATB is active. Turn hooks/ticks
    # (on_turn_start/end, turn status ticks, schedule_on) fire for the actor
    # exactly as before — only the ROUND layer is removed.
    "atb_enabled": {
        "default": False,
        "schema": {"type": "bool"},
        "desc": (
            "Master switch for the Active Time Battle turn model. When True, "
            "rounds are disabled (round_number()/turn_index()/schedule() raise; "
            "on_round_* hooks, round status ticks, and round schedules never "
            "fire) and next_turn picks the entity whose ATB bar fills soonest "
            "instead of cycling turn order. Design a system around this from "
            "the start — it changes the fundamental turn flow."
        ),
    },
    "atb_charge_formula": {
        "default": "entity[self].initiative",
        "schema": {"type": "str"},
        "desc": (
            "Formula EXPRESSION giving an entity's per-tick ATB charge RATE "
            "(`self` = the entity). The bar fills by this rate; the entity "
            "with the least time-to-fill ((atb_threshold - bar) / rate) acts "
            "next. Default reuses the initiative var (every turn-order member "
            "has one), so initiative doubles as speed out of the box. Compose "
            "richer rates with vars the entity actually has (a missing var "
            "RAISES — guard with var_has or give it a default via !defvar): "
            "e.g. `entity[self].speed + entity[self].haste * 2`. A rate <= 0 "
            "means the entity can't charge (never acts). Only when atb_enabled."
        ),
    },
    "atb_threshold": {
        "default": 100,
        "schema": {"type": "int"},
        "desc": (
            "The ATB bar value an entity must reach to take a turn. Higher = "
            "slower cadence overall. Per-entity pace is expressed via the "
            "charge rate, not here. Only consulted when atb_enabled."
        ),
    },
    "atb_reset_formula": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "What happens to the actor's ATB bar after it takes a turn. EMPTY "
            "(default) = the built-in: subtract atb_threshold (keeping any "
            "overflow toward the next turn). Set a formula PROGRAM (`self` = "
            "the actor) to override — e.g. `entity[self].atb_charge = 0` to "
            "drop overflow, or subtract more for a recovery penalty. Read the "
            "threshold via atb_threshold(). Runs for a skipped turn too."
        ),
    },
    "atb_charge_var": {
        "default": "atb_charge",
        "schema": {"type": "str"},
        "desc": (
            "Name of the entity var holding the ATB charge bar. It's an "
            "ordinary var, so a GM formula can read it (var_get) or nudge it "
            "(a 'haste' effect that adds charge, an 'ambush' that pre-fills "
            "it). Absent = 0. Only used when atb_enabled."
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
    "status_default_stack": {
        "default": "refresh",
        "schema": {
            "type": "enum",
            "choices": ["refresh", "add_level", "extend", "replace", "none"],
        },
        "desc": (
            "Default re-application behavior for apply_status / `!status "
            "apply` when a status is applied to an entity that already has "
            "it, used when the status DEFINITION doesn't set its own "
            "`stack`. Modes: 'refresh' (reset duration, keep level), "
            "'add_level' (level += applied, capped at the definition's "
            "max_level, and refresh duration), 'extend' (add to remaining "
            "duration, keep level), 'replace' (overwrite level + duration), "
            "'none' (re-application is a no-op while the status is present). "
            "A first application (status not yet present) always just sets "
            "the given level/duration regardless of mode."
        ),
    },
    "status_resist_sources": {
        "default": "equipped",
        "schema": {"type": "str"},
        "desc": (
            "Comma-separated vars subtree roots scanned for status "
            "resistance/immunity records (like modifier_sources). A nested "
            "`status_resist` (map of status-name-or-`tag:<x>` -> integer "
            "level reduction) and/or `status_immune` (list / CSV of names + "
            "`tag:<x>`) found under a scanned root contributes to the bearer "
            "— so an EQUIPPED resistance ring works while one sitting in the "
            "inventory does nothing. A DIRECT `status_immune` / `status_resist` "
            "var on the entity (innate) always counts. Per-entity overrides: "
            "`__status_resist_sources` var (replaces this list) and/or "
            "`__status_resist_sources_add` var (extends it)."
        ),
    },
    "status_resist_stack": {
        "default": "sum",
        "schema": {
            "type": "enum",
            "choices": ["sum", "max", "first"],
        },
        "desc": (
            "How multiple matching status_resist reductions combine for one "
            "incoming status: 'sum' (add them — two +1 rings = +2), 'max' "
            "(take the single strongest), 'first' (the first record found "
            "wins). Immunity is independent — any matching status_immune "
            "entry blocks the application outright regardless of this."
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
    # ---- Movement blocking (impassable tiles / zones) ----
    # An entity attempting to enter a cell is BLOCKED when the tile or any
    # zone covering that cell evaluates its block condition truthy for that
    # mover. Conditions are formula EXPRESSIONS with `entity[self]` = the
    # moving entity, so blocking can be conditional ("a short wall blocks
    # unless entity[self].flying; an indoor wall blocks unless
    # entity[self].ghost"). Resolution per cell: the tile's own `block`
    # data field wins, else its template's `block`, else the
    # tile_block_condition rule (the same instance > template > system
    # layering as a tile glyph). Zones: the zone's `block` data field,
    # else zone_block_condition. A block value may be a formula STRING
    # (evaluated) or a bare bool/number (true = always impassable). A
    # malformed formula — OR one reading a var the mover lacks — fails
    # OPEN (does NOT block) so a GM typo can't soft-lock the board; give
    # the gating vars a default via default_entity_vars (!defvar) so the
    # read always resolves. Empty = nothing blocks (default).
    "tile_block_condition": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Default formula deciding whether a TILE blocks an entity from "
            "entering its cell. Bindings: tile_x / tile_y (read the tile's "
            "data via tile_get/tile_has) and entity[self] = the moving "
            "entity (read its vars, e.g. entity[self].flying — define the "
            "var via !defvar so the read resolves). Truthy = blocked. This "
            "is the fallback; a tile's own `block` data field (or its "
            "template's) overrides it per cell — set with `!tile set <x> "
            "<y> block \"<formula>\"`. Empty = tiles never block. Malformed "
            "formula = treated as not blocking. Which MOVEMENT kinds honor "
            "blocking is set by the block_walk / block_tp / block_push / "
            "block_swap rules (all default True)."
        ),
    },
    "zone_block_condition": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Default formula deciding whether a ZONE blocks an entity from "
            "entering any of its cells. Bindings: zone_name and entity[self] "
            "= the moving entity. Truthy = blocked. A zone's own `block` "
            "data field overrides it (`!zone set <name> block \"<formula>\"`). "
            "Empty = zones never block. Malformed = not blocking. A cell is "
            "blocked if its tile OR any covering zone blocks the mover."
        ),
    },
    "corpse_block_condition": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Default formula deciding whether a CORPSE blocks an entity from "
            "entering its cell (corpses are passable by default — empty rule). "
            "Bindings: tile_x / tile_y (the corpse cell), corpse_id (read the "
            "dead entity's frozen vars via corpse_var(corpse_id, ...)), "
            "corpse_team, and entity[self] = the moving entity. Truthy = "
            "blocked — e.g. `corpse_var(corpse_id, 'footprint_w', 1) >= 2` so "
            "only big corpses block, or `not entity[self].flying`. A cell is "
            "blocked if ANY corpse covering it blocks the mover; a large "
            "corpse blocks its whole footprint. Malformed = not blocking. "
            "Honored by the same block_walk / block_tp / block_push / "
            "block_swap movement-kind toggles as tile/zone blocking."
        ),
    },
    "anchored_zone_on_anchor_loss": {
        "default": "delete",
        "schema": {"type": "enum", "choices": ["delete", "freeze", "suspend"]},
        "desc": (
            "What happens to an entity-anchored aura zone (one bound via "
            "`!zone anchor <name> <eid> <radius>`) when its anchor entity "
            "dies, is destroyed (a body part hit to 0 hp), or leaves the "
            "match. 'delete' (default) removes the zone entirely — the aura "
            "vanishes with its source. 'freeze' clears the anchor binding but "
            "leaves the cells where they last were, turning the aura into an "
            "ordinary static zone (a lingering cloud the caster left behind). "
            "'suspend' clears the aura's cells (it goes inert — no render, no "
            "hooks, no membership) but KEEPS the anchor binding, so the aura "
            "automatically RESUMES (re-stamps around the anchor) if that "
            "entity is revived from its corpse or that part is healed above 0. "
            "Note: a true despawn (`!ent remove`) under 'suspend' leaves an "
            "inert bound zone that won't resume (nothing to revive) — re-anchor "
            "or delete it manually."
        ),
    },
    # ---- mounts / vehicles ----
    # Who is the action's `source` when a RIDER triggers a vehicle action
    # (a slot-granted action, or a vehicle action whose allowed_slots
    # includes the rider's slot). 'rider' (default) runs it as the rider
    # (source = the rider) with a `vehicle` binding pointing at the host;
    # 'vehicle' runs it as the vehicle (source = the host) with a `rider`
    # binding pointing at the triggering occupant. Either way BOTH bindings
    # are available in the body. Per-vehicle override: the vehicle's
    # `mount_action_actor` var.
    "mount_action_actor": {
        "default": "rider",
        "schema": {"type": "enum", "choices": ["rider", "vehicle"]},
        "desc": (
            "Who acts when a rider triggers a vehicle/slot action: 'rider' "
            "(source = the occupant, with a `vehicle` binding) or 'vehicle' "
            "(source = the host, with a `rider` binding). Both bindings are "
            "always available; this only sets which is `source`. Override per "
            "vehicle with its `mount_action_actor` var."
        ),
    },
    # What happens to a vehicle's riders when the vehicle dies / leaves the
    # match. 'eject' (default) dismounts everyone to free cells near the
    # vehicle's last position (offboard but alive); 'kill' runs the kill
    # function on each rider; 'keep' leaves them mounted for manual cleanup.
    "mount_on_host_death": {
        "default": "eject",
        "schema": {"type": "enum", "choices": ["eject", "kill", "keep"]},
        "desc": (
            "Fate of riders when their vehicle dies or despawns: 'eject' "
            "(dismount to nearby free cells), 'kill' (run the kill function "
            "on each), or 'keep' (leave them mounted for manual cleanup)."
        ),
    },
    # What happens to a vehicle's riders when the vehicle TRANSFORMS into a
    # form whose slots don't accommodate every current rider (a rider's slot
    # is missing in the new form). If EVERY rider's slot still exists in the
    # new form, they stay mounted regardless of this rule. 'block' (default)
    # refuses the transform (raises, no change); 'eject' dismounts every rider
    # to free cells near the vehicle, then transforms.
    "transform_rider_mismatch_mode": {
        "default": "block",
        "schema": {"type": "enum", "choices": ["block", "eject"]},
        "desc": (
            "When transforming/polymorphing a VEHICLE whose new form lacks a "
            "slot for some current rider: 'block' (refuse the transform, no "
            "change) or 'eject' (dismount all riders to nearby cells, then "
            "transform). If the new form has matching slots for ALL riders, "
            "they stay mounted regardless of this rule."
        ),
    },
    # Whether a HIDDEN rider (a passenger in a slot with no `region`, tucked
    # inside the vehicle) contributes to its team's vision / fog reveal.
    # Default False — symmetric with a hidden rider being excluded from
    # being SEEN and from the map (the vehicle's own vision still applies);
    # a passenger inside a transport grants no sight. Set True for "every
    # crew member's eyes count" systems. Only affects TEAM vision (fog,
    # team_sees_*); an explicit can_see(<rider>, ...) query still works.
    "hidden_rider_grants_vision": {
        "default": False,
        "schema": {"type": "bool"},
        "desc": (
            "Whether a hidden rider (passenger in a region-less slot, tucked "
            "inside the vehicle) adds to its team's vision and fog reveal. "
            "Default False (symmetric with being hidden from the map); the "
            "vehicle's own vision still applies. An explicit per-entity "
            "can_see query on the rider is unaffected."
        ),
    },
    # Roster suffix appended to a mounted rider's !list / !state row.
    # Placeholders: {vehicle}, {vehicle_name}, {slot}. Empty = off.
    "mount_entity_line_suffix": {
        "default": " [riding {vehicle} :: {slot}]",
        "schema": {"type": "str"},
        "desc": (
            "Suffix appended to a mounted rider's roster row. Placeholders: "
            "{vehicle}, {vehicle_name}, {slot}. Empty = off."
        ),
    },
    # What a GROUP move (!ent move group:NAME ...) does with a member that is
    # RIDING a vehicle — its position is controlled by the vehicle, not the
    # group. 'skip' (default) silently excludes such riders from the group
    # move (they stay aboard; if their vehicle is itself in the group it
    # carries them); 'abort' refuses the whole move, naming the riders.
    "mount_group_move_mode": {
        "default": "skip",
        "schema": {"type": "enum", "choices": ["skip", "abort"]},
        "desc": (
            "Group-move handling of a mounted member: 'skip' (exclude the "
            "rider from the move — it's carried by its vehicle) or 'abort' "
            "(refuse the whole group move, naming the riders)."
        ),
    },
    # Which movement kinds honor tile/zone blocking. Each defaults True
    # (block everything); set a kind False to let it phase through walls
    # (e.g. block_tp=False lets teleports ignore blocking). The low-level
    # move_to primitive (raw force placement from actions) and
    # spawn/summon are never gated by these.
    "block_walk": {
        "default": True,
        "schema": {"type": "bool"},
        "desc": (
            "Whether stepwise walking (!ent move) honors tile/zone "
            "blocking. A walk whose path crosses a blocked cell is "
            "refused entirely (all-or-nothing, like hitting an occupied "
            "cell). Set False to let walking ignore blocking."
        ),
    },
    "block_tp": {
        "default": True,
        "schema": {"type": "bool"},
        "desc": (
            "Whether teleport (!ent tp) honors tile/zone blocking. Set "
            "False to let teleports phase through / onto walls."
        ),
    },
    "block_push": {
        "default": True,
        "schema": {"type": "bool"},
        "desc": (
            "Whether forced movement (push / pull) honors tile/zone "
            "blocking — a pushed entity stops at the cell before a blocked "
            "one (knockback into a wall). Set False to shove through."
        ),
    },
    "block_swap": {
        "default": True,
        "schema": {"type": "bool"},
        "desc": (
            "Whether !ent swap honors tile/zone blocking — a swap is "
            "refused if either entity would land on a cell that blocks it. "
            "Set False to allow swapping into otherwise-blocked cells."
        ),
    },
    # ---- Map resize ----
    "map_resize_shrink_mode": {
        "default": "block",
        "schema": {"type": "enum", "choices": ["block", "kill"]},
        "desc": (
            "What `!map resize` does when shrinking would push a live "
            "entity outside the new grid (after the anchor shift). 'block' "
            "(default) refuses the whole resize and lists the entities in "
            "the way — nothing changes. 'kill' runs the configured kill "
            "function (default_kill_function_effects + the corpse/delete "
            "death pipeline) on each out-of-bounds entity, then completes "
            "the resize. Out-of-bounds TILES and CORPSES are dropped and "
            "zone cells clipped either way (only entities trigger block)."
        ),
    },
    # ---- Directional / facing-relative geometry ----
    "directional_corner_arc": {
        "default": 30,
        "schema": {"type": "int"},
        "desc": (
            "Default angular width (degrees) of each diagonal CORNER side "
            "when the directional primitives (side_hit / relative_side / "
            "directional_get) run in 8-way mode (sides=8). Each of the four "
            "corners (front_right_side, back_right_side, back_left_side, "
            "front_left_side) spans this many degrees centered on its 45° "
            "diagonal; the four cardinal faces (front/back/left_side/"
            "right_side) each span the remaining 90 − arc. 0 collapses to "
            "4-way (corners vanish); 45 gives eight equal octants. Default "
            "30 makes a square's corners narrower targets than its flat "
            "faces. Per-call override: pass corner_arc= to any directional "
            "primitive. 4-way mode ignores this (faces are always 90°)."
        ),
    },
    "event_recursion_limit": {
        "default": 64,
        "schema": {"type": "int"},
        "desc": (
            "Max nesting depth for the custom event bus (emit). A handler "
            "fired by an event may itself emit; this caps the chain so a "
            "handler that re-emits its own event can't loop forever. Beyond "
            "the limit, further emit() calls in the chain are suppressed (a "
            "single warning is logged). Mirrors var_hook_recursion_limit."
        ),
    },
    "macro_repeat_limit": {
        "default": 1000,
        "schema": {"type": "int"},
        "desc": (
            "Max iterations a single `repeat N` block in a macro will run "
            "(N is clamped to this). Guards against a typo'd huge count."
        ),
    },
    "macro_step_limit": {
        "default": 10000,
        "schema": {"type": "int"},
        "desc": (
            "Hard backstop on the TOTAL number of command lines a single "
            "`!macro run` may dispatch (across all loops/branches). Stops a "
            "pathologically nested macro from hanging; the run aborts with an "
            "error when exceeded."
        ),
    },
    "side_hit_hitbox_mode": {
        "default": "box",
        "schema": {"type": "enum", "choices": ["box", "center"]},
        "desc": (
            "How side_hit / directional_get / hit_location resolve which side "
            "of a MULTI-TILE target a hit lands on. `box` (default): the "
            "footprint is treated as a real rectangle — the bearing from the "
            "body's center to the attacker is aspect-corrected by the "
            "footprint's half-extents, so a hit along a LONG flank reads as a "
            "side (not front) even near a corner. `center`: the legacy bearing "
            "from the footprint center as a point (a long body's corners read "
            "front-ish). 1×1 entities are identical under both — the aspect "
            "correction is uniform when width == height. Per-call override: "
            "pass hitbox='box'|'center' to side_hit / directional_get / "
            "hit_location."
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
    "body_part_entity_line_suffix": {
        "default": " [part of {parent}]",
        "schema": {"type": "str"},
        "desc": (
            "Appended to the entity_line_format row (in !list / !state) for a "
            "SUB-ENTITY — an entity attached as a body part / segment of "
            "another (its `part_of` points at a live parent). Lets a roster "
            "show at a glance which body a part belongs to. Placeholders: "
            "{parent} (parent id), {parent_name} (parent's name), plus every "
            "key entity_line_format exposes (resolved against the PART). Empty "
            "= no suffix. Only parts that appear on the roster (located / "
            "segment / region parts; glued parts are hidden) ever show it."
        ),
    },
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
    "alive_condition": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Formula expression deciding whether an entity reads as ALIVE — "
            "the gate behind Entity.is_alive, which governs render, occupancy, "
            "and the spatial / roster enumerators (distinct from "
            "death_condition, which triggers the death PIPELINE). Runs with "
            "`self` bound to the entity. EMPTY (default) uses the built-in "
            "rule: present when `hp > 0` OR the entity is indestructible (a "
            "0/0 passthrough body part / zone that never dies on its own) — so "
            "such a part still renders and occupies. Set a formula to "
            "customize (it REPLACES the built-in, so include the carve-out "
            "yourself if wanted, e.g. `entity[self].hp > 0 or "
            "is_indestructible(self) or entity[self].undying`). Malformed / "
            "erroring formula falls back to the built-in rule (never blanks "
            "the board). Evaluated only when set, so the default stays on the "
            "fast path."
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
    # ---- Visibility / per-channel POV -----------------------------------
    # A formula EXPRESSION evaluated once per entity when a render is asked
    # for from a non-omniscient (team) point of view. Returns truthy ->
    # the entity is VISIBLE to that POV; falsy -> hidden (dropped from the
    # map glyph layer and the !state/!list entity rosters for that
    # channel). Default "" means "no visibility rule" — every entity is
    # always visible, so behavior is unchanged until a GM opts in.
    #
    # Bindings: `self` = the entity under test; `pov_team` = the viewing
    # channel's team (a string). Omniscient views (host channels, and
    # `!state full` / `!map full`) SKIP this formula entirely and show
    # everything, so the formula only ever runs with a concrete team.
    #
    # The engine hardcodes no notion of "invisible" or "stealth" — those
    # live in entity data + this formula. Typical patterns:
    #   "is_part_of_team(self, pov_team) or not status_has(self, 'hidden')"
    #     — your own team is always visible; others hide while 'hidden'.
    #   "not status_has(self, 'invisible')"
    #     — a flat stealth flag, same for every team.
    # A malformed formula is treated as VISIBLE, so a GM typo reveals
    # rather than blanking the whole board.
    "entity_visibility_condition": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Formula expression deciding whether an entity is visible to "
            "a team POV (a player channel). Truthy = visible, falsy = "
            "hidden. Bindings: self = the entity, pov_team = the viewing "
            "team. Empty (default) = everything visible. Omniscient views "
            "(host channels / !state full / !map full) bypass it. "
            "Malformed formula = treated as visible. Example: "
            "\"is_part_of_team(self, pov_team) or not status_has(self, "
            "'hidden')\"."
        ),
    },
    # Fake-statblock / disguise (116): the entity var holding a display-only,
    # POV-gated disguise dict {name?, glyph?, glyphs?, color?, vars?}. A viewer
    # NOT on the entity's own team (and not omniscient) sees the disguise's
    # name/glyph/color and its `vars` overlaid on the roster; allies and the
    # GM/omniscient view see the truth. Engine mechanics (targeting, formulas,
    # damage) ALWAYS use the real statblock — a disguise only changes what's
    # rendered. A decoy/illusion is a GM composition on top of this.
    "disguise_var": {
        "default": "disguise",
        "schema": {"type": "str"},
        "desc": (
            "Entity var holding a display-only, POV-gated disguise dict "
            "{name?, glyph?, glyphs?, color?, vars?}. Non-allied, "
            "non-omniscient viewers see the disguise (name/glyph/color + the "
            "`vars` overlay on the roster); the entity's own team and the GM "
            "see the real statblock. Mechanics always use the real vars."
        ),
    },
    # Per-tile visibility (hidden traps, secret doors, etc.). Same shape
    # as entity_visibility_condition but for the TILE glyph layer + the
    # !tile list/info/cells listings. Bindings: pov_team = the viewing
    # team, tile_x / tile_y = the tile's coordinates (use tile_get /
    # tile_has(tile_x, tile_y, "path") to inspect its data). No `self`
    # (a tile isn't an entity). Empty (default) = every tile visible.
    # Example — a trap stays hidden until the team has detected it (the
    # GM stores a per-team flag on the tile):
    #   "not tile_has(tile_x, tile_y, 'trap') or
    #    tile_has(tile_x, tile_y, 'detected.' + pov_team)"
    "tile_visibility_condition": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Formula expression deciding whether a TILE (its glyph on the "
            "map and its !tile list/info/cells rows) is visible to a team "
            "POV. Truthy = visible. Bindings: pov_team = viewing team, "
            "tile_x / tile_y = the tile coords (inspect data via "
            "tile_get/tile_has(tile_x, tile_y, ...)). Empty (default) = "
            "all tiles visible. Omniscient views / full reveals bypass "
            "it. Malformed = treated as visible. Example: \"not "
            "tile_has(tile_x, tile_y, 'trap') or tile_has(tile_x, "
            "tile_y, 'detected.' + pov_team)\"."
        ),
    },
    # Per-zone visibility (hidden regions: an unseen gas cloud, a secret
    # room). Filters the ZONE glyph layer + the !zone listings. Bindings:
    # pov_team = viewing team, zone_name = the zone (inspect via
    # zone_get/zone_has(zone_name, "path")). No `self`. Empty (default) =
    # every zone visible.
    "zone_visibility_condition": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Formula expression deciding whether a ZONE (its glyph on the "
            "map and its !zone listing rows) is visible to a team POV. "
            "Truthy = visible. Bindings: pov_team = viewing team, "
            "zone_name = the zone (inspect via zone_get/zone_has("
            "zone_name, ...)). Empty (default) = all zones visible. "
            "Omniscient / full reveals bypass it. Malformed = visible."
        ),
    },
    # Per-corpse visibility (the Dead: section of !list / !state). A
    # corpse is a STORED SNAPSHOT, not a live entity, so the formula
    # can't bind `self` or read entity[X] vars; instead it gets the dead
    # entity's team plus the corpse coords. Bindings: pov_team = viewing
    # team, corpse_team = the dead entity's team value (the team_var on
    # its death snapshot; "" if none), tile_x / tile_y = the corpse's
    # tile. Empty (default) = all corpses visible. Example — only your
    # own team's corpses show: "corpse_team == pov_team".
    "corpse_visibility_condition": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Formula expression deciding whether a CORPSE row (Dead: "
            "section of !list/!state) is visible to a team POV. Truthy = "
            "visible. Bindings: pov_team = viewing team, corpse_team = "
            "the dead entity's team at death (\"\" if none), tile_x / "
            "tile_y = the corpse tile. (A corpse is a snapshot, not a "
            "live entity — entity[X]/self are NOT available; key off "
            "corpse_team / coords.) Empty (default) = all corpses "
            "visible. Omniscient / full reveals bypass it. Malformed = "
            "visible. Example: \"corpse_team == pov_team\"."
        ),
    },
    # ---- Fog of war (range-only spatial visibility) ---------------------
    # A spatial visibility layer ON TOP of the formula-driven conditions
    # above. When a match has fog ON (per-match Match.fog_enabled, seeded
    # from fog_enabled_by_default) and a channel renders from a TEAM POV,
    # the engine hides anything in a cell that team can't currently see
    # and paints fog_glyph over unseen cells. "See" = within vision range
    # (fog_range_mode) of at least one ALIVE team member — vision is the
    # UNION across the team (a scout reveals for everyone). Each unit
    # always sees its own cell, so you always see your own team. RANGE
    # ONLY: no line-of-sight / opacity (a future piece). Omniscient POV
    # (host channels), `!… full`, and fog-off all bypass.
    "fog_enabled_by_default": {
        "default": False,
        "schema": {"type": "bool"},
        "desc": (
            "Whether NEW matches start with fog of war on. This only "
            "SEEDS a match's per-match fog toggle at creation; flip fog "
            "live per match with `!match fog on|off` (it's match state, "
            "not a rule, so a system refresh won't change it). Default "
            "False — fog is opt-in."
        ),
    },
    "fog_vision_radius_var": {
        "default": "fog_vision_radius",
        "schema": {"type": "str"},
        "desc": (
            "Entity var name holding each unit's vision radius for fog. A "
            "team sees every cell within this many cells (per "
            "fog_range_mode) of any alive member. Missing / non-integer "
            "var = radius 0 (sees only its own cell) — set a sensible "
            "default with `!defvar add fog_vision_radius <n>`."
        ),
    },
    "fog_range_mode": {
        "default": "square_radius",
        "schema": {
            "type": "enum",
            "choices": ["square_radius", "manhattan", "euclidean"],
        },
        "desc": (
            "Distance metric for fog vision. 'square_radius' (default, "
            "Chebyshev — a radius-N square, max(|dx|,|dy|)<=N); "
            "'manhattan' (|dx|+|dy|<=N, a diamond); 'euclidean' "
            "(dx^2+dy^2<=N^2, a disc). Same family as the distance() "
            "formula modes."
        ),
    },
    "fog_glyph": {
        "default": "?",
        "schema": {"type": "str"},
        "desc": (
            "Single character painted over every cell the POV team can't "
            "see when fog is on — drawn on TOP of terrain/zone glyphs so "
            "unseen cells read as fog, not as their real contents. Empty "
            "string = no overlay (unseen cells just render blank '.'). "
            "Only the first character is used."
        ),
    },
    # ---- graphics / sprite rendering ----
    # The engine stays pixel-agnostic: a sprite is referenced by a KEY string
    # stored in entity/tile/zone data (mirroring glyphs), resolved by the same
    # instance>template>rule + disguise/POV precedence. render_scene() emits a
    # declarative model (sprite placements + tint/opacity/flip/mode + fog +
    # borders + background) that a graphics surface (gui.py) draws; the engine
    # never loads an image. All sprite rules default to the no-sprite / text
    # behavior, so ASCII rendering is unaffected. Opacities are 0-100 percent
    # (the schema has no float type); the surface divides by 100.
    "sprite_mode": {
        "default": "single",
        "schema": {"type": "enum",
                   "choices": ["single", "stretch", "tile"]},
        "desc": (
            "How a MULTI-TILE entity's sprite fills its footprint: 'single' "
            "(one sprite at the anchor cell), 'stretch' (one sprite scaled "
            "across the whole footprint), or 'tile' (the sprite repeated once "
            "per covered cell). Per-entity override: the `sprite_mode` var. "
            "Irrelevant for 1x1 entities. Only used by render_scene."
        ),
    },
    "sprite_mirror": {
        "default": "horizontal",
        "schema": {"type": "enum",
                   "choices": ["none", "horizontal", "vertical", "both"]},
        "desc": (
            "When an entity has no `sprites.<facing>` for its current facing, "
            "which axes the renderer may MIRROR an existing facing's sprite to "
            "fill it: 'horizontal' (default — left<->right), 'vertical' "
            "(up<->down), 'both', or 'none' (never mirror; fall straight back "
            "to the base `sprite`). The model emits the chosen key plus "
            "flip_h/flip_v flags for the surface to apply. Per-entity "
            "override: the `sprite_mirror` var."
        ),
    },
    "fallback_sprite": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Sprite KEY used when an entity/tile/zone resolves no sprite of "
            "its own (and no facing-mirror). Empty = no fallback (the surface "
            "renders the glyph as text instead). A system-wide 'unknown "
            "object' placeholder sprite."
        ),
    },
    "background_sprite": {
        "default": "ground_default",
        "schema": {"type": "str"},
        "desc": (
            "System-default background sprite KEY, drawn as the BOTTOM layer "
            "(beneath zones) across the whole grid. A per-match background "
            "(`!map background <key> [mode]`) overrides this. Empty = none. "
            "Defaults to `ground_default` (a tiled ground texture from the "
            "sprites folder); if that PNG is missing the renderer paints a "
            "flat ground colour so cells stay visible."
        ),
    },
    "background_mode": {
        "default": "tile",
        "schema": {"type": "enum",
                   "choices": ["stretch", "tile", "center"]},
        "desc": (
            "How the background sprite fills the grid: 'stretch' (scale to the "
            "whole map), 'tile' (repeat), or 'center' (one copy centered). "
            "Default for the background_sprite rule (tile, so a small ground "
            "texture repeats per cell); a per-match background carries its "
            "own mode."
        ),
    },
    "fog_sprite": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Sprite KEY drawn over every cell the POV team can't see (the "
            "graphics analog of fog_glyph), at fog_opacity. Empty = the "
            "surface just dims/hides unseen cells with no sprite."
        ),
    },
    "fog_opacity": {
        "default": 60,
        "schema": {"type": "int"},
        "desc": (
            "Opacity (0-100 percent) of the graphics fog overlay over unseen "
            "cells. 100 = fully hides what's underneath; lower = translucent "
            "haze. Only used by render_scene (fog_glyph drives text fog)."
        ),
    },
    "show_borders": {
        "default": True,
        "schema": {"type": "bool"},
        "desc": (
            "Draw grid lines between tiles in the graphics surface (rendered "
            "above the ground/background but below tiles, zones, and entities "
            "for visual clarity). Color + opacity come from border_color / "
            "border_opacity; a tile may override its own edge via "
            "`border_color` / `border_opacity` data. Per-match override: "
            "`!map border on|off`."
        ),
    },
    "border_color": {
        "default": "white",
        "schema": {"type": "str"},
        "desc": (
            "Color of the grid border lines (a name or hex the surface "
            "understands, e.g. 'white' / '#FFFFFF'). Per-tile override: the "
            "tile's `border_color` data. Per-match override: `!map border "
            "color <name>`."
        ),
    },
    "border_opacity": {
        "default": 50,
        "schema": {"type": "int"},
        "desc": (
            "Opacity (0-100 percent) of the grid border lines. Default 50 (a "
            "subtle grid). Per-tile override: the tile's `border_opacity` "
            "data. Per-match override: `!map border opacity <n>`."
        ),
    },
    # ---- graphics: sprite Z-LAYER (draw order) per kind ----
    # The render-scene compositor draws placements low-to-high. Background is
    # always the floor (drawn first); these set each kind's draw layer. A
    # higher number draws ON TOP. Per-ITEM override: an entity's `sprite_layer`
    # var or a tile's `sprite_layer` data field (a number) wins over the rule.
    "sprite_layer_zone": {
        "default": 25,
        "schema": {"type": "int"},
        "desc": (
            "Z-layer (draw order) for zone sprites/tints in the graphics "
            "surface. Higher = on top. Background is the floor (always below). "
            "Defaults: zone 25 < tile 50 < corpse 75 < entity 100 < rider 110."
        ),
    },
    "sprite_layer_tile": {
        "default": 50,
        "schema": {"type": "int"},
        "desc": (
            "Z-layer (draw order) for special-tile sprites in the graphics "
            "surface. Higher = on top. Per-tile override: the tile's "
            "`sprite_layer` data field."
        ),
    },
    "sprite_layer_corpse": {
        "default": 75,
        "schema": {"type": "int"},
        "desc": (
            "Z-layer (draw order) for corpse sprites in the graphics surface "
            "(default below living entities so a body reads as on the ground)."
        ),
    },
    "sprite_layer_entity": {
        "default": 100,
        "schema": {"type": "int"},
        "desc": (
            "Z-layer (draw order) for entity sprites in the graphics surface. "
            "Higher = on top. Per-entity override: the entity's `sprite_layer` "
            "var (e.g. a flier drawn over everyone)."
        ),
    },
    "sprite_layer_rider": {
        "default": 110,
        "schema": {"type": "int"},
        "desc": (
            "Z-layer (draw order) for visible riders and region body-parts, "
            "drawn over their host/parent entity. Higher = on top. Per-entity "
            "override: the part/rider's `sprite_layer` var."
        ),
    },
    "sprite_layer_overlay": {
        "default": 150,
        "schema": {"type": "int"},
        "desc": (
            "Default Z-layer for OVERLAY sprites drawn over an entity (status "
            "FX like a burning overlay, plus the entity's overlay var). 150 = "
            "above entities (100). Per-overlay override: a status definition's "
            "`sprite_layer` (or an overlay record's `layer`)."
        ),
    },
    "overlay_var": {
        "default": "overlays",
        "schema": {"type": "str"},
        "desc": (
            "Entity var holding ad-hoc overlay sprites drawn over the entity "
            "(in addition to status overlays). A dict of records keyed by name "
            "— each a sprite KEY string or `{sprite, opacity, tint, layer}`. A "
            "passive/action composes an overlay by writing this var (e.g. "
            "`entity[self].overlays.flame.sprite = \"flame\"`)."
        ),
    },
    "corpse_sprite_tint": {
        "default": "gray",
        "schema": {"type": "str"},
        "desc": (
            "Tint the surface applies to a corpse's sprite so a body reads as "
            "dead — default 'gray' (desaturate). Empty = no tint. A corpse "
            "renders the dead entity's own stored sprite at this tint + "
            "corpse_sprite_opacity."
        ),
    },
    "corpse_sprite_opacity": {
        "default": 50,
        "schema": {"type": "int"},
        "desc": (
            "Opacity (0-100 percent) of a corpse's sprite. Default 50 (a "
            "semi-transparent body). Combined with corpse_sprite_tint."
        ),
    },
    "sprite_cell_size": {
        "default": 100,
        "schema": {"type": "int"},
        "desc": (
            "Pixel size of one grid cell in the graphics surface (gui.py). "
            "Default 100 -> a 100x100 px cell. A sprite is scaled to its "
            "footprint x this size. Engine-agnostic (text rendering ignores "
            "it); the surface reads it."
        ),
    },
    # ---- map viewport (panning) + legend ----
    # The viewport caps how much map is shown at once (for surfaces with
    # limited width, like Discord): when EITHER grid dimension exceeds its
    # cap, only a window of that size renders and the channel pans it around.
    # A grid that fits both caps renders whole (no viewport).
    "viewport_width": {
        "default": 30,
        "schema": {"type": "int"},
        "desc": (
            "Max map columns shown at once before a horizontal viewport "
            "engages. A grid wider than this renders a window and the "
            "channel pans it (`!map pan left|right`)."
        ),
    },
    "viewport_height": {
        "default": 30,
        "schema": {"type": "int"},
        "desc": (
            "Max map rows shown at once before a vertical viewport engages "
            "(see viewport_width)."
        ),
    },
    # Whether the viewport applies on a given surface. 'auto' (default)
    # defers to the surface: Discord (narrow) opts IN, the CLI / harness
    # (which fit wide output) opt OUT. 'on' forces the viewport on every
    # surface (e.g. to pan a huge map in the CLI); 'off' disables it
    # everywhere (always render the whole grid).
    "viewport_mode": {
        "default": "auto",
        "schema": {"type": "enum", "choices": ["auto", "on", "off"]},
        "desc": (
            "When the map viewport engages: 'auto' (Discord on, CLI/harness "
            "off), 'on' (all surfaces), or 'off' (never — always render the "
            "whole grid). Caps are viewport_width / viewport_height."
        ),
    },
    # How many tiles a single Discord arrow-button click pans the viewport
    # (the buttons move in bigger steps than the command, which pans an
    # exact count). 0 = half the viewport in that axis (a "half-screen"
    # scroll that scales with the window). The `!map pan <dir> [n]` command
    # always pans exactly n (default 1) regardless of this.
    "viewport_button_step": {
        "default": 0,
        "schema": {"type": "int"},
        "desc": (
            "Tiles per Discord pan-button click (0 = half the viewport — a "
            "half-screen scroll). The `!map pan <dir> [n]` command pans an "
            "exact n instead."
        ),
    },
    # Auto-legend: a glyph->meaning key appended under the map. Seeds a new
    # match's per-match toggle (flip live with `!map legend on|off`); a
    # one-off `!map legend` / `!map nolegend` arg overrides per render.
    "map_legend_by_default": {
        "default": False,
        "schema": {"type": "bool"},
        "desc": (
            "Whether NEW matches start with the auto-legend on (a "
            "glyph->meaning key under the map covering the entities / tiles "
            "/ zones / fog actually visible in the current view). Toggle "
            "live with `!map legend on|off`; force per render with the "
            "`!map legend` / `!map nolegend` argument."
        ),
    },
    # Fog MEMORY (explored terrain). When on, cells a team has ever seen
    # stay revealed even after they leave current vision (the team
    # "remembers" the lay of the land), instead of the map snapping back
    # to only what's currently in range. The explored set accumulates as
    # units move and reveal new cells, and is per-team + persisted.
    "fog_memory_enabled_by_default": {
        "default": False,
        "schema": {"type": "bool"},
        "desc": (
            "Whether NEW matches start with fog MEMORY on. Only SEEDS a "
            "match's per-match memory toggle at creation; flip it live per "
            "match with `!match fog memory on|off` (match state, survives "
            "a rule refresh). Default False — explored fog resets to "
            "current vision each time (no memory). Memory has no effect "
            "unless fog itself is on (fog_enabled_by_default / !match "
            "fog)."
        ),
    },
    "fog_memory_mode": {
        "default": "full",
        "schema": {"type": "enum", "choices": ["full", "terrain"]},
        "desc": (
            "What a REMEMBERED cell (explored but not currently in vision) "
            "shows, when fog memory is on. 'full' (default): everything "
            "currently there, including live entities — memory = 'once "
            "seen, stays visible'. 'terrain': only static features "
            "(tiles, zones, corpses) are remembered; LIVE entities still "
            "require current vision, so you can't track moving units "
            "through explored-but-unwatched areas (classic fog of war). "
            "Currently-visible cells always show everything regardless."
        ),
    },
    # ---- Line of sight / opacity ----
    # Sight is blocked by OPAQUE cells. Opacity is a per-cell formula
    # EXPRESSION (NOT action mode — read viewer vars via entity[self].x),
    # the sight analogue of the movement `block` system and resolved the
    # same way: a tile's own `opaque` data field > its template's `opaque`
    # > tile_opaque_condition; a zone's `opaque` data > zone_opaque_condition.
    # `self` is the VIEWER, so opacity can be conditional ("a wall blocks
    # sight unless entity[self].true_sight"; "low wall unless tall"). A
    # value may be a formula string OR a bare bool/number (opaque=true =
    # always blocks). Fail-TRANSPARENT (a typo/missing var does NOT blind).
    # These conditions + the has_los / can_see / team_sees_* formula
    # primitives are ALWAYS live; the fog_los rule only governs whether the
    # auto-fog feature factors LOS in.
    "tile_opaque_condition": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Default formula deciding whether a TILE blocks line of sight "
            "through its cell. Bindings: tile_x / tile_y (read the tile's "
            "data via tile_get/tile_has) and entity[self] = the VIEWER. "
            "Truthy = opaque. Overridden per cell by a tile's own `opaque` "
            "data field (or its template's) — `!tile set <x> <y> opaque "
            "\"<formula>\"`. Empty = tiles never block sight. Malformed / "
            "missing-var = transparent. SEPARATE from tile_block_condition "
            "(movement) — set each independently (a window: opaque empty + "
            "block true; smoke: opaque true + block empty)."
        ),
    },
    "zone_opaque_condition": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Default formula deciding whether a ZONE blocks line of sight "
            "through any of its cells. Bindings: zone_name, tile_x / tile_y, "
            "entity[self] = the viewer. Overridden by a zone's own `opaque` "
            "data field (`!zone set <name> opaque \"<formula>\"`). A cell "
            "blocks sight if its tile OR any covering zone is opaque."
        ),
    },
    "los_corner_mode": {
        "default": "permissive",
        "schema": {"type": "enum",
                   "choices": ["permissive", "strict", "open"]},
        "desc": (
            "How a line of sight that runs exactly through a grid CORNER "
            "(diagonally between four cells) is judged. 'permissive' "
            "(default): blocked only when BOTH cells flanking the corner "
            "are opaque (an 'X' of two walls) — a lone diagonal pillar does "
            "NOT cast a shadow. 'strict': any single opaque cell touched at "
            "the corner blocks (isolated pillars shadow their diagonal). "
            "'open': corner touches never block (a sightline may slip "
            "between two diagonal walls). Only affects exact-diagonal "
            "corners; cells the line passes through normally always block."
        ),
    },
    "fog_los": {
        "default": False,
        "schema": {"type": "bool"},
        "desc": (
            "Whether the auto-fog feature factors LINE OF SIGHT in (in "
            "addition to range). False (default): fog is range-only — a "
            "cell is seen if any alive team member is within vision radius. "
            "True: a member must ALSO have an unobstructed line (no opaque "
            "cell between) — walls hide what's behind them, the map shows "
            "fog_glyph there, and explored memory only records cells "
            "actually seen. System-wide because a system either models "
            "sightlines or it doesn't; whether a given match is foggy is "
            "the per-match !match fog toggle. The raw has_los / can_see / "
            "team_sees_* primitives work regardless of this rule."
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
    "action_choice_limit": {
        "default": 20,
        "schema": {"type": "int"},
        "desc": (
            "Maximum number of mid-body choices (choose / choose_number "
            "prompts) a single action invocation may make. The runner "
            "re-runs the body once per choice (replay model), so this "
            "bounds that work and catches a body that loops choose() "
            "unboundedly. Hitting it raises an engine fault. Raise it if "
            "you legitimately need more interactive picks in one action."
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
    "default_entity_vars": {
        "default": {},
        "schema": {"type": "dict"},
        "desc": (
            "Map of var-path -> default value applied to every entity at "
            "creation (add / summon / revive — all route through "
            "Entity.spawn), filling ONLY vars the entity doesn't already "
            "have. Applied at the START of spawn, before vital-var "
            "validation, so a default can even satisfy a required var "
            "(e.g. hp). Values are typed (coerced like !ent set_var: "
            "true/false -> bool, ints, floats, else string); dotted paths "
            "create nested dicts. Fill-only — an entity that already has "
            "the var keeps its value, so it's injection not enforcement. "
            "Edit via !defvar add/remove/list, not !system set. The "
            "intended home for things like a fog vision radius "
            "(`!defvar add fog_vision_radius 4`)."
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


RESERVED_IDS: Set[str] = {"current", "this", "self", "parent"}

# Recognized modifier fold ops (see Match._apply_modifier_op). An op outside
# this set still folds as a lenient add, but `!mod show` flags it as a likely
# typo via Match.unknown_modifier_ops.
MODIFIER_OPS: Tuple[str, ...] = ("add", "inc%", "more%", "set", "min", "max")


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
    # Mount lifecycle (mounts/vehicles). Fire on the RIDER entity (`self`
    # = the rider; `this` = current-turn entity). Context bindings:
    #   vehicle   — the host vehicle's id (new HOOK_CONTEXT name)
    #   slot      — the slot name the rider entered / left
    # on_mounted fires after a rider boards (or switches slots — the
    # switch fires on_dismounted from the old slot then on_mounted in the
    # new); on_dismounted fires after a rider leaves (also on host-death
    # eject). Lets the GM grant a "buckled in" status, deduct fuel, etc.
    "on_mounted",
    "on_dismounted",
}

# Custom event-bus subscription. A passive whose `when` is "event:<name>"
# (any non-empty name) fires when the GM emits that named event via the
# emit() formula primitive or the !emit command — a user-extensible hook
# surface decoupling cause from effect, on top of the fixed HOOK_NAMES.
_EVENT_WHEN_PREFIX = "event:"


def is_event_hook(when: Any) -> bool:
    """True iff `when` is a custom event-bus subscription ('event:<name>')."""
    return (isinstance(when, str)
            and when.startswith(_EVENT_WHEN_PREFIX)
            and len(when) > len(_EVENT_WHEN_PREFIX))

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
# zones[name]["data"] is free-form GM data). `anchor`/`anchor_radius`/
# `anchor_metric` make a zone an entity-anchored AURA: its cells are
# re-stamped (a radius around the anchor entity's footprint) whenever
# the anchor moves. They're reserved so `!zone set` / zone_set can't
# clobber the binding.
ZONE_RESERVED_KEYS = frozenset({
    "cells", "data", "hooks", "glyph",
    "anchor", "anchor_radius", "anchor_metric",
})

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
        if self.when not in HOOK_NAMES and not is_event_hook(self.when):
            allowed = ", ".join(sorted(HOOK_NAMES))
            raise VTTError(
                f"Unknown passive hook '{self.when}'. Allowed: {allowed}; "
                f"or a custom event subscription 'event:<name>'."
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
            out[k] = copy.deepcopy(v) if isinstance(v, dict) else {}
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

    # Locational damage: when this entity is a BODY PART of another entity,
    # `part_of` holds the parent entity's id. A part is a real Entity (it
    # gets hp/vars/statuses/passives/death for free) but is "attached" — it
    # mirrors the parent's position and is skipped by the map-facing surface
    # (render, occupancy, distance/AoE/zone/vision enumeration, the !list
    # roster). None = an ordinary, independent entity. Set via `!part
    # attach`/`detach` (a protected field, NOT a var) so it survives a rules
    # refresh and can't be casually set_var'd. See section 7 of CLAUDE.md.
    part_of: "str | None" = field(default=None)

    # Mounts / vehicles: when this entity is RIDING another (a vehicle),
    # `mounted_on` holds the host vehicle's id and `mount_slot` the slot
    # name it occupies. A rider is a normal independent entity (keeps its
    # own hp/turn/actions) that is carried by the vehicle's movement and
    # hidden from / excluded from the ground (occupancy + spatial targeting)
    # while aboard. The slot DEFINITIONS live on the vehicle (its `slots`
    # var); this back-link is the per-rider occupancy fact, derived-scanned
    # the same way `part_of` is. Protected fields (NOT vars) so they survive
    # a rules refresh and can't be casually set_var'd. None = not riding.
    mounted_on: "str | None" = field(default=None)
    mount_slot: "str | None" = field(default=None)

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
        m = self._match
        # Configurable override: the alive_condition rule (a formula
        # expression with `self` = this entity). Empty (default) uses the
        # built-in rule below. Evaluated only when set, so the default stays
        # on the fast path; guarded against recursion (a condition that calls
        # an is_alive-using enumerator) via _alive_eval_depth; malformed /
        # erroring formula falls back to the built-in (never blanks the board).
        if m is not None and m._alive_eval_depth == 0:
            cond = str(m.rules.get("alive_condition", "")).strip()
            if cond:
                from formula import FormulaEngine, EvalCtx, FormulaError
                m._alive_eval_depth += 1
                try:
                    return bool(FormulaEngine(m).eval_expression(
                        cond, EvalCtx(this=self.id, target=self.id)))
                except FormulaError:
                    pass
                finally:
                    m._alive_eval_depth -= 1
        # Built-in rule: present when hp > 0, OR the entity is INDESTRUCTIBLE —
        # a 0/0 passthrough body part / zone (e.g. a Destroyer segment routing
        # all damage to main) sits on the board at 0 hp and never dies on its
        # own, so it must still render / occupy / enumerate.
        if self.hp > 0:
            return True
        return m is not None and m.is_indestructible(self)

    @property
    def is_part(self) -> bool:
        """True when this entity is an attached body part of another entity
        (its `part_of` points at a live parent). Attached parts mirror the
        parent's position and are skipped by the map-facing surface
        (render, occupancy, distance/AoE/zone/vision enumeration, the
        !list roster). The link is ignored if the parent no longer exists
        (a dangling part reads as an ordinary entity)."""
        po = self.part_of
        return bool(po) and self._match is not None and po in self._match.entities

    @property
    def is_located_part(self) -> bool:
        """True for a body part that lives on its OWN cell rather than being
        glued to the parent's (the `__part_located` var). It still routes
        damage / resolves `parent` / dies with the parent, but it is NOT
        re-stamped to the parent's position and NOT hidden from the map —
        it renders, occupies, is targetable, and sees/is-seen normally (a
        turret you can flank separately)."""
        return self.is_part and bool(self.vars.get("__part_located"))

    @property
    def is_segment(self) -> bool:
        """True for a snake/worm body SEGMENT (the `__segment` var). A segment
        is a LOCATED part (own cell — renders, occupies, is targetable) that
        ALSO follows the head along the body chain (`__follows` points at the
        segment/head directly ahead). The follow movement, the
        segment_death_mode sever behaviors, and the self-collision toggle are
        what distinguish it from a plain located part."""
        return self.is_part and bool(self.vars.get("__segment"))

    @property
    def is_region_part(self) -> bool:
        """True for a body part auto-positioned over a REGION of the parent's
        footprint (the `__part_region` var: front/back/left/right/center/all
        + corners, facing-aware). Its anchor follows the parent but its cells
        are the region (derived by Match.part_region_cells), so a blast on the
        parent's front hits the front part. On the map (not hidden) but
        attached. Mutually exclusive with __part_located (located wins)."""
        return (self.is_part and not self.vars.get("__part_located")
                and bool(self.vars.get("__part_region")))

    @property
    def is_glued_part(self) -> bool:
        """A body part glued to the parent's ANCHOR cell (the default) —
        mirrors the parent's position and is skipped by the map-facing
        surface. The skip-surface predicate; LOCATED and REGION parts are on
        the map normally."""
        return (self.is_part and not self.vars.get("__part_located")
                and not self.vars.get("__part_region"))

    @property
    def is_mounted(self) -> bool:
        """True when this entity is RIDING a vehicle (its `mounted_on`
        points at a live host). A rider keeps its own hp/turn/actions but is
        carried by the vehicle's movement and is off the ground while
        aboard. The link is ignored if the host no longer exists (a dangling
        rider reads as an ordinary entity)."""
        mo = self.mounted_on
        return bool(mo) and self._match is not None and mo in self._match.entities

    @property
    def is_hidden_rider(self) -> bool:
        """A rider in a slot with NO `region` — tucked inside the vehicle:
        hidden from the map, off the ground (no occupancy), and excluded
        from spatial targeting (the part-glued skip surface). A rider in a
        slot WITH a region (is_visible_rider) renders/targets at that
        footprint cell instead."""
        if not self.is_mounted:
            return False
        sd = self._match.slot_def(self.mounted_on, self.mount_slot)
        return not (isinstance(sd, dict) and str(sd.get("region", "")).strip())

    @property
    def is_visible_rider(self) -> bool:
        """A rider whose slot declares a `region` — shown and targetable at
        that facing-relative footprint cell of the vehicle (a gunner on top,
        a rider in the saddle), but still carried by the vehicle and not
        blocking the ground."""
        if not self.is_mounted:
            return False
        sd = self._match.slot_def(self.mounted_on, self.mount_slot)
        return bool(isinstance(sd, dict) and str(sd.get("region", "")).strip())

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

    def _mount_move_redirect(self) -> "Entity":
        """Resolve who actually moves when THIS entity is told to move
        (tp / move_dirs). When not riding, returns self. When riding a slot
        flagged `controls_movement`, returns the VEHICLE (the driver steers
        the whole rig; riders are carried via _restamp_riders_for). When
        riding a non-control slot, raises — a passenger can't move on its
        own and must dismount first."""
        if not self.is_mounted:
            return self
        m = self._match
        sd = m.slot_def(self.mounted_on, self.mount_slot) or {}
        if sd.get("controls_movement"):
            return m.entities[self.mounted_on]
        raise VTTError(
            f"'{self.id}' is riding '{self.mounted_on}' in slot "
            f"'{self.mount_slot}' and can't move on its own — dismount "
            f"first, or ride a slot that controls movement."
        )

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

        # Fill missing vars from the system's default_entity_vars BEFORE
        # validating placement and vital vars — a default may legitimately
        # supply the required hp var OR the footprint size, so the
        # footprint-aware bounds/occupancy check below must see the
        # defaults. Fill-only, so an explicitly-provided value wins.
        match._apply_default_vars(self)

        # Validate the WHOLE footprint anchored at (x, y): every covered
        # cell in bounds and unoccupied (stackable spawners skip
        # occupancy — same rule as tp / move_dirs / summon). Spawn is
        # never gated by movement-block (mode=None), matching the
        # "spawn/summon are never block-gated" convention.
        match._validate_placement(self, x, y, None)

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
        # Fog memory: a freshly-spawned unit reveals cells for its team.
        match._record_vision(getattr(self, "team", None))
        return (self.id, log)

    def remove(self):
        """
        Remove this entity from its match and turn order.
        Also scrubs the entity from every group it was a member of, so
        no group is left holding a dangling id.
        """
        m = self._require_match()
        # Resolve any riders this entity was carrying (death OR despawn both
        # route here) per the mount_on_host_death rule — done while `self` is
        # still in the match so the eject search can use its footprint.
        m._release_riders(self.id)
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
        # Resolve any aura zones anchored to this entity (death OR despawn
        # both route through here): delete or freeze per the
        # anchored_zone_on_anchor_loss rule.
        m._release_anchored_zones(self.id)
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
        # Mounts: a rider's own move is redirected to the VEHICLE when its
        # slot controls movement (driving), or refused when it's a plain
        # passenger. The vehicle carries everyone via _restamp_riders_for.
        mover = self._mount_move_redirect()
        if mover is not self:
            return mover.tp(x, y, fire_hooks=fire_hooks)
        # Validate the WHOLE destination footprint in one shot: every
        # covered cell in bounds, unoccupied (stackable movers skip
        # occupancy — they may enter cells holding non-stackable
        # entities), and not tp-blocked. (x, y) is the top-left anchor.
        m._validate_placement(self, x, y, "tp")
        log: List[str] = []
        old_x, old_y = self.x, self.y
        # Footprint cell sets at the old and new anchors (the dims don't
        # change during a tp, so new_cells is just old_cells shifted).
        old_cells = m.entity_cells(self, old_x, old_y)
        new_cells = m.entity_cells(self, x, y)
        if fire_hooks and (old_x, old_y) != (x, y):
            # Skip on_exit when teleporting to the current footprint (no
            # actual move). Matches the intuition that "tp to where I
            # am" is a no-op rather than a fire-and-reverse cycle.
            log.extend(m.fire_footprint_tile_exit(self.id, old_cells, new_cells))
            log.extend(m.fire_footprint_zone_exit(self.id, old_cells, new_cells))
        self.move_to(x, y)
        if fire_hooks:
            log.extend(m.fire_footprint_tile_enter(self.id, old_cells, new_cells))
            log.extend(m.fire_footprint_tile_stop(self.id, new_cells))
            # A tp is a single, final step; zone enter + stop fire here.
            # (For a same-cell tp, fire_footprint_zone_enter still fires
            # on_stop/on_cell_stop for the standing-on zones — a tp that
            # "lands" re-affirms its stop, matching on_stop's tile
            # semantics which fire even on a same-cell tp.)
            log.extend(m.fire_footprint_zone_enter(self.id, old_cells, new_cells, True))
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
                  fire_hooks: bool = True,
                  block_mode: Optional[str] = "walk") -> List[str]:
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
        # Mounts: redirect a driver's walk to the vehicle, or refuse a
        # passenger's (see tp). Done before path-building so the whole walk
        # applies to the vehicle's footprint.
        mover = self._mount_move_redirect()
        if mover is not self:
            return mover.move_dirs(moves, fire_hooks=fire_hooks,
                                   block_mode=block_mode)
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
                # Validate the WHOLE swept footprint each step: every
                # covered cell must be in bounds, and (unless block_mode
                # is None) none may block this mode. Checking the full
                # footprint — not just the anchor — is what stops a large
                # entity squeezing through a gap narrower than its body.
                # A failure ANYWHERE on the path fails the whole walk
                # (all-or-nothing, like an occupied final cell). block_mode
                # None skips the block check — callers that pre-validated
                # the path (push/pull) pass None to avoid re-checking.
                for cx, cy in m.entity_cells(self, nx, ny):
                    if not m.in_bounds(cx, cy):
                        raise OutOfBounds(
                            f"({cx},{cy}) outside {m.grid_width}x{m.grid_height}")
                if block_mode is not None:
                    for cx, cy in m.entity_cells(self, nx, ny):
                        if m._check_block(self.id, cx, cy, block_mode):
                            raise Blocked(f"Cell ({cx},{cy}) blocks `{self.id}`")
                step_path.append((nx, ny, step_facing))
                x, y = nx, ny
        # Stackable movers skip the final-cell occupancy check (see
        # Entity.tp for the symmetric rationale). move_dirs only
        # validates the FINAL footprint for occupancy (intermediate
        # cells may be passed through, matching the 1×1 semantics), so
        # this is the one occupancy gate; a large entity's whole
        # destination footprint must be clear.
        if not self.is_cell_stackable:
            ig = m._occupancy_ignore(self)
            for cx, cy in m.entity_cells(self, x, y):
                if m.cell_occupant(cx, cy, ig) is not None:
                    raise Occupied(f"Cell ({cx},{cy}) already occupied")

        # Phase 2: commit each step, firing hooks. No more validation —
        # phase 1 already proved the whole path is legal.
        log: List[str] = []
        origin_x, origin_y = self.x, self.y
        for nx, ny, step_facing in step_path:
            step_from_x, step_from_y = self.x, self.y
            old_cells = m.entity_cells(self, step_from_x, step_from_y)
            new_cells = m.entity_cells(self, nx, ny)
            if fire_hooks:
                # Per-step, fire on_exit/on_cell_exit for the cells the
                # footprint vacates this step and the boundary on_exit for
                # zones it fully leaves.
                log.extend(m.fire_footprint_tile_exit(self.id, old_cells, new_cells))
                log.extend(m.fire_footprint_zone_exit(self.id, old_cells, new_cells))
            self.facing = step_facing
            self.move_to(nx, ny)
            if fire_hooks:
                # on_enter/on_cell_enter for the newly-covered cells; the
                # stop hooks fire once after the loop at the final cells.
                log.extend(m.fire_footprint_tile_enter(self.id, old_cells, new_cells))
                log.extend(m.fire_footprint_zone_enter(
                    self.id, old_cells, new_cells, False))
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
            # on_stop fires once per final footprint cell, even if no actual
            # movement happened (zero-step move_dirs) — empty step_path
            # means no transit and therefore no stop.
            final_cells = m.entity_cells(self, self.x, self.y)
            log.extend(m.fire_footprint_tile_stop(self.id, final_cells))
            log.extend(m.fire_footprint_zone_stop(self.id, final_cells))
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
            # deepcopy, NOT dict(): vars hold nested dicts (inventory,
            # modifiers, …) that _set_path mutates IN PLACE. A shallow copy
            # would share those nested objects with the live entity, so a
            # later nested write would corrupt this snapshot — breaking undo
            # and action rollback for any dotted-path var. (status above is
            # deepcopied for the same reason.)
            "vars": copy.deepcopy(self.vars),
            "passives": {pid: p.to_dict() for pid, p in self.passives.items()},
            "clamps": {path: c.to_dict() for path, c in self.clamps.items()},
            "facing": self.facing,
            "part_of": self.part_of,
            "mounted_on": self.mounted_on,
            "mount_slot": self.mount_slot,
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
            vars=copy.deepcopy(data.get("vars", {})),
            facing=data.get("facing", "up"),
            part_of=data.get("part_of", None),
            mounted_on=data.get("mounted_on", None),
            mount_slot=data.get("mount_slot", None),
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

    # Status DEFINITIONS — self-describing statuses (the "rich status"
    # feature). name -> {tick, tick_when, stack, max_level, data}. A
    # status instance on an entity (entity.status[name]) resolves its
    # behavior from the definition of the SAME name: at tick time the
    # engine runs the definition's `tick` formula when its `tick_when`
    # matches (default turn_end). Statuses with no matching definition
    # fall back to the global status_tick_formula rule (backwards compat).
    # Application/stacking goes through apply_status, honoring the
    # definition's `stack` mode (else the status_default_stack rule).
    # Edits propagate live (the instance only stores dynamic state like
    # level/duration). Serialized with the match.
    status_definitions: Dict[str, Dict[str, Any]] = field(default_factory=dict)

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

    # Per-match fog-of-war toggle. Seeded at match creation from the
    # `fog_enabled_by_default` rule, then independent of the game system
    # (toggled live via `!match fog on|off`) — it lives in its OWN field,
    # NOT in `rules`, so a system rule refresh can't flip it, the same
    # reasoning as access_overrides. When True (and a channel renders from
    # a team POV, i.e. pov_team is not None), the engine hides anything in
    # a cell that team can't currently see (union of its units' vision
    # radii) and paints the fog_glyph over unseen cells. Persisted.
    fog_enabled: bool = False

    # Per-match fog MEMORY toggle (only meaningful when fog_enabled).
    # Seeded at creation from fog_memory_enabled_by_default, then match
    # state (toggled via `!match fog memory on|off`, survives refresh).
    # When True, `explored` accumulates every cell each team has seen and
    # those cells stay revealed (per fog_memory_mode) even out of current
    # vision. When False, only current vision shows. Persisted.
    fog_memory: bool = False
    # Per-team set of explored cells: team-name -> set of (x, y) ever seen
    # by that team. Grows as units move/spawn (Match._record_vision) while
    # fog_memory is on; cleared when memory is toggled off. Serialized as
    # lists of [x, y]. Runtime type is set for fast membership tests.
    explored: Dict[str, "set"] = field(default_factory=dict)

    # ---- fog reveals (scout / clairvoyance) ----
    # Per-team list of reveal records {cells: set[(x,y)], until: Optional[int]}
    # that force cells visible to a team independent of unit vision (a scout
    # ping, clairvoyance, GM reveal). `until` is the round number the reveal
    # is active THROUGH (a reveal expires once round_number > until); None =
    # permanent. A revealed cell shows terrain AND live entities (current-
    # vision-equivalent). Set via `!reveal_fog`. Serialized.
    fog_reveals: Dict[str, "list"] = field(default_factory=dict)

    # ---- text-renderer customization ----
    # Per-team text color for the colorized renderer: team-name -> a
    # TEXT_COLORS name. An entity's own `color` var overrides this; absent
    # here, a team whose NAME is itself a palette color (e.g. "red") still
    # auto-colors. Edited via `!map teamcolor`. Serialized.
    team_colors: Dict[str, str] = field(default_factory=dict)
    # Per-match color toggle. When False, the renderer never emits color
    # even on a color-capable surface. Default on; flipped by `!map color
    # on|off`. Serialized. (Color also requires the surface to declare
    # supports_color — the scenario harness doesn't, so scenarios stay
    # plain regardless.)
    color_enabled: bool = True

    # ---- map viewport (panning) + auto-legend ----
    # Per-CHANNEL viewport offset: channel_key -> [x, y], the top-left
    # anchor of that channel's render window. Auto-created on the first pan
    # / view command, clamped so the window stays on the grid; only used
    # when the viewport engages (a grid dimension exceeds its cap and the
    # surface opts in). Per-channel so different channels pan independently
    # (the panning analog of per-channel POV). Serialized.
    channel_views: Dict[str, List[int]] = field(default_factory=dict)
    # Per-match auto-legend toggle (seeded from the map_legend_by_default
    # rule at creation; flip with `!map legend on|off`). When on, a
    # glyph->meaning key is appended under the rendered map. Serialized.
    map_legend_enabled: bool = False

    # ---- reusable named macros ----
    # name -> a body of command lines (newline-separated). Run via `!macro
    # run <name> [args...]`, which substitutes $1/$2/.../$@ and dispatches
    # each line under one undo entry. Per-match; serialized.
    macros: Dict[str, str] = field(default_factory=dict)

    # ---- named random tables ----
    # name -> a roll_table spec ("key:weight,..." CSV). Rolled by name via the
    # `table_roll(name)` formula primitive or `!table roll <name>`. Per-match;
    # serialized. The named-resource companion to inline roll_table().
    tables: Dict[str, str] = field(default_factory=dict)

    # ---- condition-watchers (edge-triggered triggers) ----
    # name -> {"condition": <formula expr>, "effect": <formula program>,
    # "once": bool, "last": bool}. Re-checked after each command + at turn/
    # round boundaries; the effect fires when condition goes false->true.
    # `last` is runtime edge-state (serialized so a reload doesn't re-fire).
    watchers: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # ---- toggleable map layers (114) ----
    # Names of render layers HIDDEN for this match (persistent, serialized).
    # Empty = everything drawn. Layers: zones / tiles / entities / fog.
    # Toggled with `!map layer <name> on|off`; a one-off `!map hide=...`
    # arg hides extra layers for a single render without storing them.
    hidden_layers: "set[str]" = field(default_factory=set)

    # ---- graphics: per-match background image ----
    # None = use the background_sprite rule (if any). Else a dict
    # {"sprite": <key>, "mode": stretch|tile|center} drawn as the bottom
    # render-scene layer. Set via `!map background <key> [mode]`. Serialized.
    background: Optional[Dict[str, Any]] = None

    # ---- graphics: per-match grid-border override ----
    # Each None = fall through to the show_borders / border_color /
    # border_opacity rules; a non-None value overrides that rule for THIS
    # match. Set via `!map border ...`. Serialized.
    border_show: Optional[bool] = None
    border_color: Optional[str] = None
    border_opacity: Optional[int] = None

    # ---- graphics: per-match render mode ----
    # "text" (ASCII, the default) or "image" (graphical PNG). On a graphics-
    # capable surface (Discord with Pillow), `image` makes a plain `!map` and
    # the auto-update board render graphically; text-only surfaces (CLI /
    # harness) always fall back to ASCII. Set via `!map mode`. Serialized.
    render_mode: str = "text"

    # ---- match outcome / victory (100) ----
    # None until a winner is declared (manually via `!match win` or from a
    # GM-composed watcher/action calling declare_winner). Then a dict
    # {"winner": str, "reason": str, "round": int}. There is NO built-in
    # objective evaluator — win conditions are composed from watchers +
    # declare_winner, keeping "victory is declared manually" the default.
    outcome: Optional[Dict[str, Any]] = None

    # ---- team-level state (resources + team-scoped modifiers) ----
    # team -> free-form data dict (command points, morale, a `modifiers`
    # bundle that applies to every member, etc.). Read/written via team_get
    # / team_set / team_add. Serialized.
    team_data: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # team -> {pid: Passive}: passives that fire for events on any member of
    # the team (self = the member), alongside global + entity passives.
    team_passives: Dict[str, Dict[str, "Passive"]] = field(default_factory=dict)

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
    # One-time latch: warn once per match session that on_round_* logic is
    # dormant while ATB is active. Reset when ATB is found off in next_turn.
    _atb_round_warned: bool = field(default=False, repr=False, compare=False)
    # Whether the most-recently-selected ATB actor was skipped (skips_turn) —
    # so its turn_END fires only status ticks (decay), not its action hooks.
    _atb_last_skipped: bool = field(default=False, repr=False, compare=False)

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
    # Mid-body action choices (choose / choose_number). The top-level
    # action runner seeds `_choice_answers` (from `answer=` invocation
    # tokens, then grows it one entry per interactive prompt) and resets
    # `_choice_cursor` to 0 before each replay attempt; the choose()
    # bindings consume answers in order. Runtime-only, preserved across
    # an action's rollback retries (see action._rollback_match).
    _choice_answers: List[Any] = field(default_factory=list, repr=False, compare=False)
    _choice_cursor: int = field(default=0, repr=False, compare=False)
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
    # Re-entry guard for the alive_condition formula. is_alive is called
    # by render / occupancy / the spatial enumerators; a custom
    # alive_condition that itself calls one of those (entities_within, ...)
    # would recurse. While >0, is_alive falls back to the built-in rule.
    # Not serialized — transient.
    _alive_eval_depth: int = field(default=0, repr=False, compare=False)
    # Transient memo for _fog_team_sees, active (a dict) only for the duration
    # of one read-only cell-scanning pass (set/torn-down around render_ascii).
    # Never held across a mutation, so it cannot go stale. None = inactive.
    _vision_memo: Optional[Dict[Any, bool]] = field(
        default=None, repr=False, compare=False)
    # Custom event bus: re-entry guard + the current-event payload stack
    # (read by event_get / event_has during handler firing). Transient — an
    # event is a transient broadcast, nothing about it is serialized.
    _event_depth: int = field(default=0, repr=False, compare=False)
    _event_stack: List[Dict[str, Any]] = field(
        default_factory=list, repr=False, compare=False)
    # Recursion-warning buffer (drained by the top-level emit), mirroring the
    # var-hook warning latch so a limit hit deep in a chain still surfaces.
    _event_warned: bool = field(default=False, repr=False, compare=False)
    _event_warnings: List[str] = field(
        default_factory=list, repr=False, compare=False)
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

    @staticmethod
    def _shift_snake_path_vars(vars_dict: Dict[str, Any], ox: int, oy: int) -> None:
        """Shift the engine-managed snake-trail coordinate vars by (ox, oy):
        __seg_path (a list of [x,y] cells the head has occupied) and __seg_last
        ([x,y], the head's last-seen cell). No-op if absent/malformed. These are
        the only coordinate-bearing ENTITY VARS the engine owns, so resize_grid
        and cross-match copy must shift them like every other spatial structure
        — otherwise a path-mode snake re-lays its body at stale cells after the
        shift."""
        if ox == 0 and oy == 0:
            return
        path = vars_dict.get("__seg_path")
        if isinstance(path, list):
            for p in path:
                if isinstance(p, list) and len(p) == 2:
                    p[0] += ox
                    p[1] += oy
        last = vars_dict.get("__seg_last")
        if isinstance(last, (list, tuple)) and len(last) == 2:
            vars_dict["__seg_last"] = [last[0] + ox, last[1] + oy]

    def resize_grid(self, new_w: int, new_h: int,
                    anchor: str = "top-left") -> Tuple[Dict[str, Any], List[str]]:
        """Resize the grid to new_w x new_h, repositioning ALL content
        (entities, tiles, corpses, zones, fog-explored memory) by an offset
        derived from `anchor` — the 9-point compass point where existing
        content stays put. top-left (default) leaves coordinates unchanged
        and grows/cuts at the bottom-right; bottom-right shifts everything
        so it stays anchored to the far corner; center recenters; etc.

        Shrinking that would push a live entity off the new grid is governed
        by the map_resize_shrink_mode rule: 'block' (default) raises with the
        offending ids and changes nothing; 'kill' runs the configured kill
        function on them and proceeds. Out-of-bounds tiles/corpses are
        dropped and zone cells clipped regardless. Returns (summary, log)."""
        if not isinstance(new_w, int) or isinstance(new_w, bool) or \
           not isinstance(new_h, int) or isinstance(new_h, bool):
            raise VTTError("resize: width and height must be integers.")
        if new_w < 1 or new_h < 1:
            raise VTTError("resize: width and height must be at least 1.")
        key = str(anchor).strip().lower()
        if key not in _RESIZE_ANCHORS:
            raise VTTError(
                f"resize: unknown anchor '{anchor}'. Use one of: top-left, "
                f"top, top-right, left, center, right, bottom-left, bottom, "
                f"bottom-right."
            )
        h_kind, v_kind = _RESIZE_ANCHORS[key]
        dw, dh = new_w - self.grid_width, new_h - self.grid_height
        ox = 0 if h_kind == "left" else (dw if h_kind == "right" else dw // 2)
        oy = 0 if v_kind == "top" else (dh if v_kind == "bottom" else dh // 2)

        def in_new(x: int, y: int) -> bool:
            return 1 <= x <= new_w and 1 <= y <= new_h

        # Entities whose post-shift footprint would fall (even partly) off
        # the new grid — a large entity is "cut" if ANY covered cell lands
        # off-grid.
        cut = sorted(e.id for e in self.entities.values()
                     if not all(in_new(cx + ox, cy + oy)
                                for cx, cy in self.entity_cells(e)))
        log: List[str] = []
        if cut:
            mode = str(self.rules.get("map_resize_shrink_mode", "block"))
            if mode != "kill":
                ids = ", ".join(f"`{i}`" for i in cut)
                raise VTTError(
                    f"Resize to {new_w}x{new_h} ({key}) would cut off "
                    f"{len(cut)} entit{'y' if len(cut) == 1 else 'ies'} "
                    f"({ids}). Move them into the kept region first, or set "
                    f"map_resize_shrink_mode=kill to kill them instead."
                )
            # kill mode: kill the cut entities before shifting. Their corpses
            # land at the current cell and are dropped below if that cell
            # shifts off-grid (which, being in the cut region, it does).
            for eid in cut:
                _, klog = self.kill_entity(eid)
                log.extend(klog)

        # Shift survivors — each is in-bounds by construction (the off-grid
        # ones were just killed or we'd have raised).
        for e in self.entities.values():
            e.x += ox
            e.y += oy
            self._shift_snake_path_vars(e.vars, ox, oy)

        # Re-key tiles (corpses ride along inside tile data); drop off-grid.
        old_tiles = len(self.tiles)
        self.tiles = {(x + ox, y + oy): cell
                      for (x, y), cell in self.tiles.items()
                      if in_new(x + ox, y + oy)}
        dropped_tiles = old_tiles - len(self.tiles)

        # Shift + clip zone cells.
        clipped_cells = 0
        for z in self.zones.values():
            cells = z.get("cells")
            if isinstance(cells, set):
                before = len(cells)
                z["cells"] = {(x + ox, y + oy) for (x, y) in cells
                              if in_new(x + ox, y + oy)}
                clipped_cells += before - len(z["cells"])

        # Shift + clip fog-explored memory.
        if self.explored:
            self.explored = {
                team: {(x + ox, y + oy) for (x, y) in cells
                       if in_new(x + ox, y + oy)}
                for team, cells in self.explored.items()
            }
        # Shift each channel's viewport CAMERA by the same offset so it keeps
        # framing the same content after a center/edge-anchored resize (the
        # camera is coordinate-bearing too). resolve_viewport re-clamps on
        # read, so an offset that lands off the (resized) grid self-corrects.
        if self.channel_views:
            for ck, off in self.channel_views.items():
                if isinstance(off, (list, tuple)) and len(off) == 2:
                    self.channel_views[ck] = [off[0] + ox, off[1] + oy]

        self.grid_width, self.grid_height = new_w, new_h
        return ({"offset": (ox, oy), "anchor": key, "killed": cut,
                 "dropped_tiles": dropped_tiles,
                 "clipped_zone_cells": clipped_cells}, log)


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
            # deepcopy data/hooks: zone_set_path / the zone_set primitive mutate
            # the zone data dict IN PLACE, so a by-reference snapshot would be
            # corrupted by a later edit (defeats undo + action rollback).
            "data": copy.deepcopy(z.get("data") or {}),
            "hooks": copy.deepcopy(z.get("hooks") or {}),
        }
        g = z.get("glyph")
        if isinstance(g, str) and len(g) == 1:
            out["glyph"] = g
        c = z.get("color")
        if isinstance(c, str) and c:
            out["color"] = c
        # Aura binding (entity-anchored zone), if present.
        if z.get("anchor"):
            out["anchor"] = z["anchor"]
            out["anchor_radius"] = int(z.get("anchor_radius", 0) or 0)
            out["anchor_metric"] = str(z.get("anchor_metric", "square_radius"))
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
        c = zdef.get("color")
        if isinstance(c, str) and c:
            z["color"] = c
        # Restore an aura binding. The cells were serialized at their
        # last-stamped positions (and re-stamp on the next anchor move),
        # so no recompute is needed at load time.
        anchor = zdef.get("anchor")
        if isinstance(anchor, str) and anchor:
            z["anchor"] = anchor
            try:
                z["anchor_radius"] = max(0, int(zdef.get("anchor_radius", 0)))
            except (TypeError, ValueError):
                z["anchor_radius"] = 0
            metric = str(zdef.get("anchor_metric", "square_radius"))
            z["anchor_metric"] = metric if metric in self._AURA_METRICS else "square_radius"
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

    # ---- entity-anchored auras --------------------------------------
    # An aura is a zone bound to an entity: its `anchor` field holds the
    # eid, `anchor_radius` a radius, `anchor_metric` the distance metric.
    # The zone's `cells` are RE-STAMPED (a footprint-aware disc around the
    # anchor) whenever the anchor moves — so `cells` stays a concrete set
    # and every existing zone query / hook / render works unchanged. The
    # aura is footprint-aware: the disc is the union of cells within
    # `anchor_radius` of ANY of the anchor's footprint cells (radius 0 =
    # exactly the footprint), matching how large-entity vision/AoE behave.

    _AURA_METRICS = ("square_radius", "manhattan", "euclidean")

    def _within_radius(self, ux: int, uy: int, x: int, y: int,
                       radius: int, metric: str) -> bool:
        dx, dy = abs(ux - x), abs(uy - y)
        if metric == "manhattan":
            return dx + dy <= radius
        if metric == "euclidean":
            return dx * dx + dy * dy <= radius * radius
        return max(dx, dy) <= radius   # square_radius (Chebyshev)

    def _stamp_anchored_zone(self, name: str) -> None:
        """Recompute an anchored zone's cells from its anchor entity's
        current footprint + radius. No-op for a non-anchored zone or one
        whose anchor entity is gone (the anchor-loss path handles that)."""
        z = self.zones.get(name)
        if not isinstance(z, dict):
            return
        eid = z.get("anchor")
        if not eid:
            return
        e = self.entities.get(eid)
        if e is None or not e.is_alive:
            return
        r = max(0, int(z.get("anchor_radius", 0) or 0))
        metric = str(z.get("anchor_metric", "square_radius"))
        new: Set[Tuple[int, int]] = set()
        for (fx, fy) in self.entity_cells(e):
            for yy in range(max(1, fy - r), min(self.grid_height, fy + r) + 1):
                for xx in range(max(1, fx - r), min(self.grid_width, fx + r) + 1):
                    if (xx, yy) in new:
                        continue
                    if self._within_radius(fx, fy, xx, yy, r, metric):
                        new.add((xx, yy))
        z["cells"] = new

    def _restamp_anchors_for(self, eid: str) -> None:
        """Re-stamp every aura anchored to `eid` (called when it moves)."""
        for name, z in self.zones.items():
            if isinstance(z, dict) and z.get("anchor") == eid:
                self._stamp_anchored_zone(name)

    # ---------- body parts (locational damage) ----------
    def entity_parts(self, parent_id: str) -> List["Entity"]:
        """Every attached part Entity whose `part_of` points at `parent_id`,
        in insertion order. Derived (no second structure to keep in sync)."""
        return [e for e in self.entities.values() if e.part_of == parent_id]

    def entity_part_subtree(self, root_id: str) -> List["Entity"]:
        """Every DESCENDANT part of `root_id` (parts, their parts, ...), in
        BFS order (a parent always precedes its children), excluding the root.
        Used by death-cascade removal and corpse snapshotting so a multi-level
        part tree (dragon -> wing -> feather) is handled whole — the direct-
        parts-only `entity_parts` would leave deeper parts orphaned. Cycle-
        guarded against a malformed part_of chain."""
        out: List["Entity"] = []
        seen = {root_id}
        frontier = [root_id]
        while frontier:
            nxt: List[str] = []
            for pid in frontier:
                for child in self.entity_parts(pid):
                    if child.id in seen:
                        continue
                    seen.add(child.id)
                    out.append(child)
                    nxt.append(child.id)
            frontier = nxt
        return out

    def part_name_var(self) -> str:
        """Var key holding a part's role name ('head', 'left_arm') — the
        handle `part(parent, name)` / aiming resolve against. Per the
        part_name_var rule (default 'part_name')."""
        return str(self.rules.get("part_name_var", "part_name"))

    def find_part(self, parent_id: str, handle: str) -> Optional["Entity"]:
        """Resolve a part of `parent_id` by either its entity id OR its
        `part_name` var (id wins on a clash). None if no match."""
        handle = str(handle)
        name_var = self.part_name_var()
        named: Optional["Entity"] = None
        for e in self.entities.values():
            if e.part_of != parent_id:
                continue
            if e.id == handle:
                return e
            if named is None and str(e.vars.get(name_var, "")) == handle:
                named = e
        return named

    def _restamp_parts_for(self, parent_id: str) -> None:
        """Glue every attached part to its parent's current anchor cell.
        Called whenever the parent moves (alongside aura restamping) so a
        part's mirrored position rides along. Walks the WHOLE part subtree
        (BFS, parents before children) so a part-of-a-part rides along too,
        and re-stamps each moved part's OWN anchored auras so a part's aura
        follows the parent (same carry shape as _restamp_riders_for). Parts
        are non-occupying, so co-locating them is always legal."""
        if parent_id not in self.entities:
            return
        for e in self.entity_part_subtree(parent_id):
            # Located parts keep their own cell — only glued parts ride along.
            if e.vars.get("__part_located"):
                continue
            par = self.entities.get(e.part_of)
            if par is None:
                continue
            if e.x != par.x or e.y != par.y:
                e.x = par.x
                e.y = par.y
                self._restamp_anchors_for(e.id)
                # A moved part carries its OWN riders too (a body part that is
                # itself a vehicle) — same carry cascade _restamp_riders_for
                # does, so a rider on a limb follows when the parent moves.
                self._restamp_riders_for(e.id)

    # ---------- snake / segmented bodies ----------
    def snake_segments(self, head_id: str) -> List["Entity"]:
        """The ordered body chain of a snake (head -> tail). Built by walking
        each segment's `__follows` back-pointer from the head; assumes a
        linear chain (one successor per node). Empty if `head_id` has no
        segments."""
        segs = [e for e in self.entities.values()
                if e.part_of == head_id and e.vars.get("__segment")]
        by_pred: Dict[str, "Entity"] = {}
        for s in segs:
            by_pred[str(s.vars.get("__follows", ""))] = s
        chain: List["Entity"] = []
        seen: set = set()
        cur = head_id
        while True:
            nxt = by_pred.get(cur)
            if nxt is None or nxt.id in seen:
                break
            chain.append(nxt)
            seen.add(nxt.id)
            cur = nxt.id
        return chain

    def is_snake_head(self, eid: str) -> bool:
        """True iff `eid` has at least one body segment attached."""
        return any(e.part_of == eid and e.vars.get("__segment")
                   for e in self.entities.values())

    def _segment_cfg(self, head: "Entity", var: str, rule: str) -> Any:
        """Resolve a per-snake setting: the head's override var if present,
        else the gamerule."""
        v = head.vars.get(var)
        return self.rules.get(rule) if v is None else v

    def _advance_snake(self, head_id: str, prev_x: int, prev_y: int) -> None:
        """Advance a snake's body one cell after the head stepped from
        (prev_x, prev_y) to its current cell. Pure position writes (no
        movement hooks), so it never recurses through fire_entity_*."""
        head = self.entities.get(head_id)
        if head is None:
            return
        chain = self.snake_segments(head_id)
        if not chain:
            return
        mode = str(self._segment_cfg(head, "__segment_follow", "segment_follow_mode"))
        if mode == "path":
            self._advance_snake_path(head, chain)
        else:
            old = [(s.x, s.y) for s in chain]
            chain[0].x, chain[0].y = prev_x, prev_y
            for i in range(1, len(chain)):
                chain[i].x, chain[i].y = old[i - 1]
        head.vars["__seg_last"] = [head.x, head.y]

    def _advance_snake_path(self, head: "Entity", chain: List["Entity"]) -> None:
        """`path` follow mode: prepend the head's current cell to the recorded
        path and place each segment `spacing` cells further back along it."""
        try:
            spacing = max(1, int(self._segment_cfg(
                head, "__segment_spacing", "segment_spacing")))
        except (TypeError, ValueError):
            spacing = 1
        path = head.vars.get("__seg_path")
        if not isinstance(path, list):
            path = []
        path.insert(0, [head.x, head.y])
        need = (len(chain) + 1) * spacing + 1
        del path[need:]
        head.vars["__seg_path"] = path
        for i, s in enumerate(chain):
            idx = min((i + 1) * spacing, len(path) - 1)
            s.x, s.y = path[idx][0], path[idx][1]

    def _resettle_snake(self, head_id: str) -> None:
        """Re-lay a snake's body in a straight line behind the head, used
        after a discontinuous head move (teleport / swap / push) where there
        were no per-cell steps to trail through."""
        head = self.entities.get(head_id)
        if head is None:
            return
        chain = self.snake_segments(head_id)
        if not chain:
            return
        try:
            spacing = max(1, int(self._segment_cfg(
                head, "__segment_spacing", "segment_spacing")))
        except (TypeError, ValueError):
            spacing = 1
        bdx, bdy = FACING_VECTORS.get(getattr(head, "facing", "up"), (0, -1))
        bdx, bdy = -bdx, -bdy            # behind = opposite of facing
        path = [[head.x, head.y]]
        for i, s in enumerate(chain):
            cx = head.x + bdx * (i + 1) * spacing
            cy = head.y + bdy * (i + 1) * spacing
            if not self.in_bounds(cx, cy):
                cx, cy = s.x, s.y        # off-grid: leave the segment put
            s.x, s.y = cx, cy
            path.append([cx, cy])
        head.vars["__seg_last"] = [head.x, head.y]
        if str(self._segment_cfg(head, "__segment_follow",
                                 "segment_follow_mode")) == "path":
            head.vars["__seg_path"] = path

    def _find_segment_cell(self, px: int, py: int,
                           bdx: int, bdy: int) -> Optional[Tuple[int, int]]:
        """A free, in-bounds cell to place a new segment near (px, py),
        preferring the cell directly 'behind' (px+bdx, py+bdy) then a ring
        around the predecessor and the behind cell."""
        cands = [(px + bdx, py + bdy)]
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                if ox or oy:
                    cands.append((px + bdx + ox, py + bdy + oy))
                    cands.append((px + ox, py + oy))
        seen = set()
        for cx, cy in cands:
            if (cx, cy) in seen:
                continue
            seen.add((cx, cy))
            if self.in_bounds(cx, cy) and self.cell_occupant(cx, cy) is None:
                return (cx, cy)
        return None

    def add_segment(self, head_id: str, seg_id: str, name: str,
                    hp: int, max_hp: int,
                    extra_vars: Optional[Dict[str, Any]] = None
                    ) -> Tuple["Entity", List[str]]:
        """Append a body SEGMENT to a snake's tail: a located part of
        `head_id` that follows the chain, placed in a free cell behind the
        current tail (or the head if first). Sets __segment / __part_located /
        __follows (= the predecessor). Raises NotFound / DuplicateId /
        VTTError (no free cell)."""
        head = self.entities.get(head_id)
        if head is None:
            raise NotFound(f"Entity '{head_id}' not found.")
        if seg_id in self._taken_entity_ids():
            raise DuplicateId(
                f"Entity id '{seg_id}' already exists in this match.")
        chain = self.snake_segments(head_id)
        pred = chain[-1] if chain else head
        bdx, bdy = FACING_VECTORS.get(getattr(head, "facing", "up"), (0, -1))
        cell = self._find_segment_cell(pred.x, pred.y, -bdx, -bdy)
        if cell is None:
            raise VTTError(
                f"No free cell behind `{pred.id}` to place the segment.")
        hp_var, mhp_var, _ = head._vital_var_names()
        svars: Dict[str, Any] = {
            hp_var: int(hp), mhp_var: int(max_hp),
            self.part_name_var(): name,
            "__part_located": True, "__segment": True, "__follows": pred.id,
        }
        if extra_vars:
            svars.update(extra_vars)
        e = Entity(id=seg_id, name=name, x=cell[0], y=cell[1],
                   vars=svars, part_of=head_id)
        _, log = e.spawn(self, cell[0], cell[1])
        head.vars.setdefault("__seg_last", [head.x, head.y])
        # Seed the path (for `path` follow mode) with the head's current cell
        # so segments reach full spacing without a warm-up lag.
        head.vars.setdefault("__seg_path", [[head.x, head.y]])
        return e, log

    def create_part(self, parent_id: str, part_id: str, name: str,
                    hp: int, max_hp: int,
                    extra_vars: Optional[Dict[str, Any]] = None
                    ) -> Tuple["Entity", List[str]]:
        """Spawn a fresh entity at `parent_id`'s cell and attach it as a
        body part. The part's role name goes in the part_name var; hp /
        max_hp use the match's vital-var names. Returns (part, spawn_log).
        Raises NotFound (no parent) / DuplicateId (id taken)."""
        parent = self.entities.get(parent_id)
        if parent is None:
            raise NotFound(f"Entity '{parent_id}' not found.")
        if part_id in self._taken_entity_ids():
            raise DuplicateId(
                f"Entity id '{part_id}' already exists in this match.")
        hp_var, mhp_var, _ = parent._vital_var_names()
        pvars: Dict[str, Any] = {hp_var: int(hp), mhp_var: int(max_hp),
                                 self.part_name_var(): name}
        if extra_vars:
            pvars.update(extra_vars)
        e = Entity(id=part_id, name=name, x=parent.x, y=parent.y,
                   vars=pvars, part_of=parent_id)
        _, log = e.spawn(self, parent.x, parent.y)
        return e, log

    def attach_part(self, parent_id: str, part_id: str) -> "Entity":
        """Link an EXISTING entity as a body part of `parent_id`, moving it
        onto the parent's cell. Raises NotFound / VTTError (self-attach or
        already a part of someone else is allowed — it just re-points)."""
        parent = self.entities.get(parent_id)
        if parent is None:
            raise NotFound(f"Entity '{parent_id}' not found.")
        p = self.entities.get(part_id)
        if p is None:
            raise NotFound(f"Entity '{part_id}' not found.")
        if part_id == parent_id:
            raise VTTError("An entity can't be a body part of itself.")
        p.part_of = parent_id
        p.x, p.y = parent.x, parent.y
        self._rebuild_turn_order()
        return p

    def detach_part(self, part_id: str) -> "Entity":
        """Unlink a body part — it becomes an ordinary free entity at its
        current (the parent's) cell, keeping all its state. A severed limb
        is just a (usually dead, 0-hp) entity on the ground; no corpse.
        Raises NotFound / VTTError (not a part)."""
        p = self.entities.get(part_id)
        if p is None:
            raise NotFound(f"Entity '{part_id}' not found.")
        if not p.part_of:
            raise VTTError(f"`{part_id}` is not a body part.")
        p.part_of = None
        self._rebuild_turn_order()
        return p

    def locate_part(self, part_id: str, x: int, y: int) -> "Entity":
        """Make a body part INDEPENDENTLY LOCATED at (x, y): it stays
        part_of-linked (damage routing, the parent token, death/revive) but
        keeps its own cell — no longer re-stamped to the parent, and visible
        on the map (renders, occupies, targetable). Enforces bounds +
        occupancy at the destination. Raises NotFound / VTTError."""
        p = self.entities.get(part_id)
        if p is None:
            raise NotFound(f"Entity '{part_id}' not found.")
        if not p.part_of:
            raise VTTError(f"`{part_id}` is not a body part.")
        had = p.vars.get("__part_located")
        p.vars["__part_located"] = True   # set first so the occupancy gate engages
        try:
            self._validate_placement(p, x, y, None)
        except VTTError:
            if had is None:
                p.vars.pop("__part_located", None)
            raise
        p.vars.pop("__part_region", None)   # locate and region are exclusive
        p.move_to(x, y)
        return p

    def glue_part(self, part_id: str) -> "Entity":
        """Re-glue a part to its parent's anchor cell — clears both the
        located and region modes; it resumes being hidden/mirrored. Raises
        NotFound / VTTError (not a part)."""
        p = self.entities.get(part_id)
        if p is None:
            raise NotFound(f"Entity '{part_id}' not found.")
        if not p.part_of:
            raise VTTError(f"`{part_id}` is not a body part.")
        p.vars.pop("__part_located", None)
        p.vars.pop("__part_region", None)
        self._restamp_parts_for(p.part_of)
        return p

    _PART_REGIONS = frozenset({
        "all", "front", "back", "left", "right", "center",
        "front_left", "front_right", "back_left", "back_right",
    })

    def region_part(self, part_id: str, region: str) -> "Entity":
        """Auto-position a part over a REGION of the parent's footprint
        (facing-aware). Clears any manual location (exclusive). Raises
        NotFound / VTTError (not a part / bad region)."""
        p = self.entities.get(part_id)
        if p is None:
            raise NotFound(f"Entity '{part_id}' not found.")
        if not p.part_of:
            raise VTTError(f"`{part_id}` is not a body part.")
        region = str(region).strip().lower()
        if region not in self._PART_REGIONS:
            raise VTTError(
                f"unknown region '{region}'. Use one of: "
                f"{', '.join(sorted(self._PART_REGIONS))}.")
        p.vars.pop("__part_located", None)        # region and locate are exclusive
        p.vars["__part_region"] = region
        self._restamp_parts_for(p.part_of)        # snap anchor to the parent
        return p

    @staticmethod
    def _region_match(region: str, fwd: float, rgt: float, eps: float) -> bool:
        front, back = fwd > eps, fwd < -eps
        right, left = rgt > eps, rgt < -eps
        if region == "front": return front
        if region == "back": return back
        if region == "left": return left
        if region == "right": return right
        if region == "front_left": return front and left
        if region == "front_right": return front and right
        if region == "back_left": return back and left
        if region == "back_right": return back and right
        return False

    def region_cells_of(self, owner: "Entity",
                        region: str) -> List[Tuple[int, int]]:
        """The cells of `owner`'s footprint matching a facing-relative
        `region` (front/back/left/right/center/all + corners). Each footprint
        cell is projected onto the owner's forward / right axes from the
        footprint center; the region selects by sign (full 8-way). `all` /
        empty = every cell; `center` = the center cell(s), falling back to ALL
        on an even (no-true-center) footprint. Always returns at least the
        owner's anchor. Shared by region body parts and vehicle slots."""
        region = str(region or "").strip().lower()
        cells = self.entity_cells(owner)
        if not region or region == "all":
            return list(cells)
        w, h = self.entity_footprint(owner)
        ccx = owner.x + (w - 1) / 2.0
        ccy = owner.y + (h - 1) / 2.0
        fdx, fdy = FACING_VECTORS.get(getattr(owner, "facing", "up"), (0, -1))
        eps = 1e-9
        if region == "center":
            center = [(gx, gy) for (gx, gy) in cells
                      if abs((gx - ccx) * fdx + (gy - ccy) * fdy) <= eps
                      and abs((gx - ccx) * (-fdy) + (gy - ccy) * fdx) <= eps]
            return center if center else list(cells)
        sel = []
        for (gx, gy) in cells:
            dx, dy = gx - ccx, gy - ccy
            fwd = dx * fdx + dy * fdy
            rgt = dx * (-fdy) + dy * fdx
            if self._region_match(region, fwd, rgt, eps):
                sel.append((gx, gy))
        return sel if sel else [(owner.x, owner.y)]

    def part_region_cells(self, p: "Entity") -> List[Tuple[int, int]]:
        """The parent-footprint cells a region part occupies, facing-aware
        (see region_cells_of). Falls back to the part's own cell if the
        parent is gone."""
        parent = self.entities.get(p.part_of) if p.part_of else None
        if parent is None:
            return [(p.x, p.y)]
        return self.region_cells_of(
            parent, str(p.vars.get("__part_region", "")))

    # ---------- mounts / vehicles -------------------------------------
    # A vehicle is just an entity carrying a `slots` var (no hardcoded
    # type). Each slot def lives under vehicle.vars.slots.<name>:
    #   {capacity, cost, condition, region, controls_movement, actions,
    #    ...} — capacity is a numeric budget (default 1); cost is a per-rider
    #   formula consuming it (default 1); condition is a valid-rider formula
    #   gate; region positions/shows the rider (else hidden inside);
    #   controls_movement lets the occupant drive; actions is an optional
    #   slot-scoped action bundle. The rider back-link is the protected
    #   Entity.mounted_on / mount_slot fields; occupancy is derived by
    #   scanning (no second structure), mirroring body parts.

    def vehicle_slots(self, vid: str) -> Dict[str, Any]:
        """The slot-definition dict of vehicle `vid` (its `slots` var), or
        {} if it has none / isn't a vehicle."""
        v = self.entities.get(vid)
        if v is None:
            return {}
        slots = v.vars.get("slots")
        return slots if isinstance(slots, dict) else {}

    def slot_def(self, vid: str, slot: Optional[str]) -> Optional[Dict[str, Any]]:
        """The definition dict for one slot, or None if absent/malformed."""
        if not slot:
            return None
        sd = self.vehicle_slots(vid).get(slot)
        return sd if isinstance(sd, dict) else None

    def is_vehicle(self, vid: str) -> bool:
        """True iff `vid` defines at least one slot."""
        return bool(self.vehicle_slots(vid))

    def vehicle_riders(self, vid: str) -> List["Entity"]:
        """Every entity currently riding vehicle `vid` (any slot)."""
        return [e for e in self.entities.values() if e.mounted_on == vid]

    def slot_occupants(self, vid: str, slot: str) -> List["Entity"]:
        """Every entity riding `vid` in the named `slot`."""
        return [e for e in self.entities.values()
                if e.mounted_on == vid and e.mount_slot == slot]

    def _eval_slot_expr(self, vid: str, rider_id: str,
                        expr: Any, fallback: Any,
                        slot: Optional[str] = None) -> Any:
        """Evaluate a slot cost/condition formula with `self` = the rider and
        `vehicle`/`slot` bindings. `slot` is the slot being EVALUATED (passed by
        the caller — NOT the rider's current mount_slot, which is None on a
        fresh mount and stale during a switch). Empty or malformed -> fallback
        (fail-OPEN: a GM typo doesn't trap or bar a rider — give gating vars
        a default via !defvar so reads resolve)."""
        expr = str(expr or "").strip()
        if not expr:
            return fallback
        from formula import FormulaEngine, EvalCtx, FormulaError
        ctx = EvalCtx(this=self.current_entity_id(), target=rider_id,
                      extras={"vehicle": vid, "rider": rider_id, "slot": slot})
        try:
            return FormulaEngine(self).eval_expression(expr, ctx)
        except FormulaError:
            return fallback

    def slot_capacity(self, vid: str, slot: str) -> float:
        """The slot's numeric capacity budget (default 1)."""
        sd = self.slot_def(vid, slot) or {}
        try:
            return float(sd.get("capacity", 1))
        except (TypeError, ValueError):
            return 1.0

    def slot_cost_of(self, vid: str, slot: str, rider_id: str) -> float:
        """What `rider_id` would consume from the slot's budget — the slot's
        `cost` formula evaluated for that rider (default 1)."""
        sd = self.slot_def(vid, slot) or {}
        val = self._eval_slot_expr(vid, rider_id, sd.get("cost", ""), 1, slot=slot)
        try:
            return float(val)
        except (TypeError, ValueError):
            return 1.0

    def slot_used_capacity(self, vid: str, slot: str,
                           ignore: Optional[str] = None) -> float:
        """Total budget currently consumed by the slot's occupants
        (optionally excluding `ignore`, used when re-checking a rider already
        seated there)."""
        return sum(self.slot_cost_of(vid, slot, e.id)
                   for e in self.slot_occupants(vid, slot)
                   if e.id != ignore)

    def slot_condition_ok(self, vid: str, slot: str, rider_id: str) -> bool:
        """Whether `rider_id` satisfies the slot's `condition` formula
        (empty/malformed = yes)."""
        sd = self.slot_def(vid, slot) or {}
        return bool(self._eval_slot_expr(vid, rider_id, sd.get("condition", ""),
                                         True, slot=slot))

    def can_mount(self, rider_id: str, vid: str,
                  slot: str) -> Tuple[bool, str]:
        """(ok, reason) for mounting `rider_id` into `vid`'s `slot`: the
        vehicle/slot exist, no self-mount or mount cycle, the rider isn't a
        body part, the condition passes, and the cost fits the remaining
        budget. The rider's OWN current occupancy of this slot is excluded
        from the used-budget total (so a re-seat / no-op doesn't self-block)."""
        rider = self.entities.get(rider_id)
        veh = self.entities.get(vid)
        if rider is None:
            return False, f"no entity '{rider_id}'."
        if veh is None:
            return False, f"no vehicle '{vid}'."
        if rider_id == vid:
            return False, "an entity can't mount itself."
        if rider.is_part:
            return False, f"'{rider_id}' is a body part — mount its owner."
        sd = self.slot_def(vid, slot)
        if sd is None:
            names = ", ".join(sorted(self.vehicle_slots(vid))) or "(none)"
            return False, f"vehicle '{vid}' has no slot '{slot}'. Slots: {names}."
        # Cycle guard: walk vid's own mount chain; it must not lead back to
        # the rider (you can't put the cart inside the horse that's on it).
        cur, seen = veh, set()
        while cur is not None and cur.mounted_on and cur.id not in seen:
            seen.add(cur.id)
            if cur.mounted_on == rider_id:
                return False, "that would create a mount cycle."
            cur = self.entities.get(cur.mounted_on)
        if not self.slot_condition_ok(vid, slot, rider_id):
            return False, (f"'{rider_id}' doesn't meet slot '{slot}'s "
                           f"rider condition.")
        cap = self.slot_capacity(vid, slot)
        used = self.slot_used_capacity(vid, slot, ignore=rider_id)
        cost = self.slot_cost_of(vid, slot, rider_id)
        if used + cost > cap + 1e-9:
            return False, (f"slot '{slot}' is full: {used:g} + {cost:g} "
                           f"would exceed capacity {cap:g}.")
        return True, ""

    def rider_cell(self, vid: str, rider: "Entity") -> Tuple[int, int]:
        """Where a rider sits: a representative cell of its slot's region (for
        a visible slot — multiple visible occupants spread across the region's
        cells by index), else the vehicle's anchor (hidden inside)."""
        veh = self.entities.get(vid)
        if veh is None:
            return (rider.x, rider.y)
        sd = self.slot_def(vid, rider.mount_slot) or {}
        region = str(sd.get("region", "")).strip()
        if region:
            cells = self.region_cells_of(veh, region)
            if cells:
                occ = self.slot_occupants(vid, rider.mount_slot)
                try:
                    idx = [e.id for e in occ].index(rider.id)
                except ValueError:
                    idx = 0
                return cells[idx % len(cells)]
        return (veh.x, veh.y)

    def _restamp_riders_for(self, vid: str) -> None:
        """Reposition every rider of `vid` onto its slot cell (raw move_to —
        no occupancy/hooks). Called when the vehicle moves and after a
        mount/dismount/slot-change. When a rider MOVES it carries its OWN
        cargo too — its parts, anchored auras, and (for a vehicle-on-vehicle)
        its own riders — so the carry propagates down a nested stack. The
        raw move_to doesn't fire fire_entity_moved, so we replay that hook's
        carry-restamps by hand. Mount cycles are prevented by can_mount, so
        the recursion terminates."""
        for rider in self.vehicle_riders(vid):
            cx, cy = self.rider_cell(vid, rider)
            if (rider.x, rider.y) != (cx, cy):
                rider.move_to(cx, cy)
                self._restamp_anchors_for(rider.id)
                self._restamp_parts_for(rider.id)
                self._restamp_riders_for(rider.id)

    def _detach_rider(self, rider: "Entity") -> List[str]:
        """Clear a rider's mount link and fire on_dismounted. Does NOT move
        the rider (callers place it). Returns hook log."""
        vid, slot = rider.mounted_on, rider.mount_slot
        rider.mounted_on = None
        rider.mount_slot = None
        log = self.fire_hook("on_dismounted", target_ids=[rider.id],
                             extras={"vehicle": vid, "slot": slot})
        return log

    def mount_entity(self, rider_id: str, vid: str, slot: str) -> List[str]:
        """Board `rider_id` into `vid`'s `slot`. Validates via can_mount
        (raises VTTError on failure). If the rider was already mounted
        elsewhere it dismounts first. Fires on_mounted. Returns hook log."""
        ok, reason = self.can_mount(rider_id, vid, slot)
        if not ok:
            raise VTTError(reason)
        rider = self.entities[rider_id]
        log: List[str] = []
        if rider.mounted_on and not (rider.mounted_on == vid
                                     and rider.mount_slot == slot):
            log.extend(self._detach_rider(rider))
        rider.mounted_on = vid
        rider.mount_slot = slot
        cx, cy = self.rider_cell(vid, rider)
        rider.move_to(cx, cy)
        # Occupancy changed (rider left the ground); rebuild turn order so a
        # large rider no longer blocks, etc. (the rider keeps its initiative).
        self._rebuild_turn_order()
        log.extend(self.fire_hook("on_mounted", target_ids=[rider_id],
                                  extras={"vehicle": vid, "slot": slot}))
        return log

    def switch_slot(self, rider_id: str, new_slot: str) -> List[str]:
        """Move an already-mounted rider to a different slot of the SAME
        vehicle (passenger -> gunner, etc.). Validated like a fresh mount.
        Fires on_dismounted (old slot) then on_mounted (new)."""
        rider = self.entities.get(rider_id)
        if rider is None or not rider.is_mounted:
            raise VTTError(f"'{rider_id}' isn't riding anything.")
        vid = rider.mounted_on
        if new_slot == rider.mount_slot:
            raise VTTError(f"'{rider_id}' is already in slot '{new_slot}'.")
        ok, reason = self.can_mount(rider_id, vid, new_slot)
        if not ok:
            raise VTTError(reason)
        log = self._detach_rider(rider)   # fires on_dismounted (old slot)
        rider.mounted_on = vid
        rider.mount_slot = new_slot
        cx, cy = self.rider_cell(vid, rider)
        rider.move_to(cx, cy)
        log.extend(self.fire_hook("on_mounted", target_ids=[rider_id],
                                  extras={"vehicle": vid, "slot": new_slot}))
        return log

    def dismount_entity(self, rider_id: str,
                        x: Optional[int] = None,
                        y: Optional[int] = None) -> List[str]:
        """Disembark a rider to a free cell. Uses (x, y) if given (must be a
        valid placement); otherwise searches outward from the vehicle for the
        nearest cell that fits the rider's footprint. Fires on_dismounted."""
        rider = self.entities.get(rider_id)
        if rider is None or not rider.is_mounted:
            raise VTTError(f"'{rider_id}' isn't riding anything.")
        veh = self.entities.get(rider.mounted_on)
        # Resolve the drop cell FIRST (rider still mounted, so it's excluded
        # from occupancy and can't block itself) — only commit if it fits.
        if x is not None and y is not None:
            self._validate_placement(rider, int(x), int(y), None)
            dest = (int(x), int(y))
        else:
            dest = self._find_dismount_cell(rider, veh)
            if dest is None:
                raise VTTError(
                    f"no free cell to dismount '{rider_id}' near the vehicle.")
        ox, oy = rider.x, rider.y
        log = self._detach_rider(rider)
        rider.move_to(*dest)
        self._rebuild_turn_order()
        log.extend(self.fire_entity_moved(rider_id, ox, oy, dest[0], dest[1]))
        return log

    def _find_dismount_cell(self, rider: "Entity",
                            veh: Optional["Entity"]) -> Optional[Tuple[int, int]]:
        """Nearest cell whose footprint fits `rider`, searched in rings
        outward from the vehicle's footprint (or the rider's current cell if
        the vehicle is gone). None if nothing fits within the grid."""
        if veh is not None:
            origins = self.entity_cells(veh)
            ox = sum(c[0] for c in origins) / len(origins)
            oy = sum(c[1] for c in origins) / len(origins)
        else:
            ox, oy = rider.x, rider.y
        candidates = []
        for gy in range(1, self.grid_height + 1):
            for gx in range(1, self.grid_width + 1):
                if self.footprint_in_bounds(rider, gx, gy) and \
                        not self._footprint_blocked_by_others(rider, gx, gy):
                    candidates.append((gx, gy))
        if not candidates:
            return None
        candidates.sort(key=lambda c: (c[0] - ox) ** 2 + (c[1] - oy) ** 2)
        return candidates[0]

    def _footprint_blocked_by_others(self, e: "Entity",
                                     ax: int, ay: int) -> bool:
        """True if some OTHER non-stackable entity covers any cell of `e`'s
        footprint anchored at (ax, ay). (Placement helper for dismount.)"""
        for cx, cy in self.entity_cells(e, ax, ay):
            occ = self.cell_occupant(cx, cy, ignore=(e.id,))
            if occ is not None:
                return True
        return False

    def _release_riders(self, vid: str, mode: Optional[str] = None) -> List[str]:
        """Resolve a vehicle's riders when it dies / despawns, per the
        mount_on_host_death rule: 'eject' (dismount to nearby cells),
        'kill' (run the kill function on each), 'keep' (leave them linked).
        Called from Entity.remove. An explicit `mode` overrides the rule (the
        transform eject path passes 'eject'). Returns log."""
        riders = self.vehicle_riders(vid)
        if not riders:
            return []
        if mode is None:
            mode = str(self.rules.get("mount_on_host_death", "eject"))
        veh = self.entities.get(vid)
        log: List[str] = []
        for rider in riders:
            if mode == "keep":
                continue
            if mode == "kill":
                # Detach first so the rider is a normal entity for the kill
                # pipeline, then run the configured kill function.
                log.extend(self._detach_rider(rider))
                try:
                    self.kill_entity(rider.id)
                except VTTError:
                    pass
                continue
            # eject (default): place at a free cell near the vehicle.
            log.extend(self._detach_rider(rider))
            dest = self._find_dismount_cell(rider, veh)
            if dest is not None:
                rider.move_to(*dest)
        if mode != "keep":
            self._rebuild_turn_order()
        return log

    # ---------- stat modifiers (derived / effective stats) ----------
    # Base stats stay plain vars (never mutated); a modifier is a data
    # record aggregated live from its source and combined on demand. See
    # the modifier_sources / modifier_op_priority / modifier_op_order rules
    # and the apply_mods / list_mods formula primitives.
    def _effective_modifier_sources(self, e: "Entity") -> List[str]:
        """The vars roots to scan for `modifiers` bundles on `e`: the
        per-entity `__modifier_sources` var (replaces the default) or the
        modifier_sources rule, plus `__modifier_sources_add` (extends)."""
        override = e.vars.get("__modifier_sources")
        if isinstance(override, list):
            roots = [str(r) for r in override]
        else:
            roots = [r.strip() for r in
                     str(self.rules.get("modifier_sources", "equipped")).split(",")
                     if r.strip()]
        add = e.vars.get("__modifier_sources_add")
        if isinstance(add, list):
            roots = roots + [str(r) for r in add]
        return roots

    @staticmethod
    def _bundle_items(bundle: Any) -> List[Tuple[str, dict]]:
        """(key, record) for each modifier record in a `modifiers` bundle —
        a LIST (key = index) or a DICT of named records (key = name; the
        form `!ent set_var hero modifiers.fireboost.op add` builds). The
        key feeds the source label for breakdowns."""
        if isinstance(bundle, list):
            return [(str(i), m) for i, m in enumerate(bundle) if isinstance(m, dict)]
        if isinstance(bundle, dict):
            return [(str(k), m) for k, m in bundle.items() if isinstance(m, dict)]
        return []

    @staticmethod
    def _mod_source(base: str, key: str) -> str:
        return f"{base}.{key}" if base else str(key)

    @staticmethod
    def _modifier_tagset(raw: Any) -> "set[str]":
        """A modifier's tag set, accepting a list OR a comma-separated
        string (so `set_var ... .tags fire,melee` works)."""
        if isinstance(raw, str):
            return {t.strip() for t in raw.split(",") if t.strip()}
        if isinstance(raw, (list, tuple)):
            return {str(t) for t in raw}
        return set()

    def _walk_modifier_bundles(self, node: Any, base: str,
                               out: List[Tuple[dict, str]]) -> None:
        """Collect every `modifiers` bundle found anywhere under `node`
        (dicts recursed; bundles not descended), tagging each record with
        its source path (`base` is the path of `node`)."""
        if isinstance(node, dict):
            if "modifiers" in node:
                for key, rec in self._bundle_items(node["modifiers"]):
                    out.append((rec, self._mod_source(base, key)))
            for k, v in node.items():
                if k == "modifiers":
                    continue
                child = f"{base}.{k}" if base else str(k)
                self._walk_modifier_bundles(v, child, out)
        elif isinstance(node, list):
            for i, it in enumerate(node):
                child = f"{base}[{i}]" if base else f"[{i}]"
                self._walk_modifier_bundles(it, child, out)

    def _raw_modifier_records(self, e: "Entity") -> List[Tuple[dict, str]]:
        """Every (record, source) contributing to `e`, aggregated live from
        its sources: each status instance's `modifiers`, the direct
        `entity.modifiers` slot, and each scan-root subtree. Source is a
        readable label (e.g. `status:burning.fireboost`, `equipped.sword.0`)
        for the list_mods breakdown."""
        out: List[Tuple[dict, str]] = []
        for sname, sdata in e.status.items():
            if isinstance(sdata, dict) and "modifiers" in sdata:
                for key, rec in self._bundle_items(sdata["modifiers"]):
                    out.append((rec, self._mod_source(f"status:{sname}", key)))
        # Team-scoped modifiers: a `modifiers` bundle in the entity's team
        # data applies to every member.
        team = e.team
        if team is not None:
            tdata = self.team_data.get(team)
            if isinstance(tdata, dict) and "modifiers" in tdata:
                for key, rec in self._bundle_items(tdata["modifiers"]):
                    out.append((rec, self._mod_source(f"team:{team}", key)))
        if "modifiers" in e.vars:
            for key, rec in self._bundle_items(e.vars["modifiers"]):
                out.append((rec, self._mod_source("modifiers", key)))
        for root in self._effective_modifier_sources(e):
            node: Any = e.vars
            ok = True
            for seg in root.split("."):
                if isinstance(node, dict) and seg in node:
                    node = node[seg]
                else:
                    ok = False
                    break
            if ok:
                self._walk_modifier_bundles(node, root, out)
        return out

    def gather_modifiers(self, eid: str, stat: str, tags: Any,
                         context: Optional[Dict[str, Any]] = None) -> List[dict]:
        """The active modifier records on `eid` for `stat` + `tags`:
        filtered by stat name, tag subset (required ⊆ query) and negative
        tags (excluded ∩ query empty) and condition, with value formulas
        resolved to numbers and each record tagged with its `source`. The
        introspection half of the system (list_mods) and the input to
        apply_modifiers' fold.

        Tag-granting: a record's `grants_tags` adds tags to the query in a
        single pre-pass (the granting record must itself pass stat +
        required tags vs the ORIGINAL query + not_tags + condition); the
        main filter then runs against the expanded tag set. Single-pass —
        a granted tag can activate other modifiers but not chain-grant."""
        e = self.entities.get(eid)
        if e is None:
            raise NotFound(f"Entity '{eid}' not found.")
        context = dict(context or {})
        tagset = {str(t) for t in (tags or [])}
        from formula import FormulaEngine, EvalCtx, FormulaError
        engine = FormulaEngine(self)
        this_id = self.current_entity_id()

        def _ev(expr: Any) -> Any:
            return engine.eval_expression(
                str(expr),
                EvalCtx(this=this_id, target=eid, extras=dict(context)),
            )

        def _cond_ok(m: dict) -> bool:
            cond = m.get("condition", "")
            if not cond:
                return True
            try:
                return bool(_ev(cond))
            except FormulaError:
                return False          # malformed condition -> inactive (fail safe)

        records = self._raw_modifier_records(e)

        # Pass 1: expand the query tag set from grants_tags (vs original tags).
        granted: "set[str]" = set()
        for m, _src in records:
            if not m.get("grants_tags"):
                continue
            if str(m.get("stat", "")) != str(stat):
                continue
            req = self._modifier_tagset(m.get("tags"))
            notg = self._modifier_tagset(m.get("not_tags"))
            if not (req <= tagset) or (notg & tagset):
                continue
            if not _cond_ok(m):
                continue
            granted |= self._modifier_tagset(m.get("grants_tags"))
        eff_tags = tagset | granted

        # Pass 2: filter + resolve against the expanded tag set.
        out: List[dict] = []
        for m, src in records:
            if str(m.get("stat", "")) != str(stat):
                continue
            req = self._modifier_tagset(m.get("tags"))
            if not req <= eff_tags:
                continue
            notg = self._modifier_tagset(m.get("not_tags"))
            if notg & eff_tags:
                continue
            if not _cond_ok(m):
                continue
            raw_val = m.get("value", 0)
            if isinstance(raw_val, str):
                try:
                    raw_val = _ev(raw_val)
                except FormulaError:
                    continue          # malformed value -> skip this modifier
            try:
                val = float(raw_val)
            except (TypeError, ValueError):
                continue
            try:
                pri = float(m.get("priority", 0) or 0)
            except (TypeError, ValueError):
                pri = 0.0
            out.append({
                "op": str(m.get("op", "add")),
                "value": val,
                "priority": pri,
                "tags": sorted(req),
                "not_tags": sorted(notg),
                "source": src,
            })
        return out

    def _modifier_op_offsets(self) -> Dict[str, float]:
        offsets: Dict[str, float] = {}
        for tok in str(self.rules.get("modifier_op_priority", "")).split(","):
            tok = tok.strip()
            if ":" in tok:
                k, _, v = tok.partition(":")
                try:
                    offsets[k.strip()] = float(v)
                except ValueError:
                    pass
        return offsets

    def _apply_modifier_op(self, running: float, op: str,
                           vals: List[float]) -> float:
        """Combine same-op modifier values onto the running value."""
        if op == "add":
            return running + sum(vals)
        if op == "inc%":
            return running * (1 + sum(vals) / 100.0)
        if op == "more%":
            for v in vals:
                running *= (1 + v / 100.0)
            return running
        if op == "set":
            return vals[-1]
        if op == "min":          # floor: result at least the highest min
            return max(running, max(vals))
        if op == "max":          # cap: result at most the lowest max
            return min(running, min(vals))
        return running + sum(vals)   # unknown op -> lenient add (see
        #   MODIFIER_OPS / unknown_modifier_ops: surfaced as a ⚠️ advisory
        #   in `!mod show` so a typo like `inc` for `inc%` is caught.

    def unknown_modifier_ops(self, mods: List[Dict[str, Any]]) -> List[str]:
        """The sorted, de-duplicated ops in `mods` (records from
        gather_modifiers) that aren't one of the recognized fold ops. An
        unrecognized op still folds (lenient add), but it's almost always a
        typo — `!mod show` flags these so the GM notices."""
        seen = []
        for m in mods:
            op = m.get("op")
            if op not in MODIFIER_OPS and op not in seen:
                seen.append(op)
        return sorted(str(o) for o in seen)

    def apply_modifiers(self, eid: str, stat: str, base: Any, tags: Any,
                        context: Optional[Dict[str, Any]] = None) -> float:
        """Effective value of `base` after `eid`'s modifiers for `stat` +
        `tags`. Folds in effective-priority tiers (priority + the per-op
        offset rule); within a tier same-op records combine, and different
        ops apply in the modifier_op_order tiebreak order."""
        try:
            running = float(base)
        except (TypeError, ValueError):
            running = 0.0
        mods = self.gather_modifiers(eid, stat, tags, context)
        if not mods:
            return self._apply_stat_cap(stat, running)
        offsets = self._modifier_op_offsets()
        order = [t.strip() for t in
                 str(self.rules.get("modifier_op_order", "")).split(",") if t.strip()]

        def order_key(op: str) -> int:
            return order.index(op) if op in order else len(order)

        for m in mods:
            m["_eff"] = m["priority"] + offsets.get(m["op"], 0.0)
        for tier in sorted({m["_eff"] for m in mods}):
            tier_mods = [m for m in mods if m["_eff"] == tier]
            for op in sorted({m["op"] for m in tier_mods}, key=order_key):
                vals = [m["value"] for m in tier_mods if m["op"] == op]
                running = self._apply_modifier_op(running, op, vals)
        return self._apply_stat_cap(stat, running)

    def _apply_stat_cap(self, stat: str, value: float) -> float:
        """Clamp the folded value to the per-stat [lo, hi] from the
        modifier_stat_caps rule (a global safety net, applied even with no
        modifiers; per-entity caps use the min/max modifier ops). Rule
        format: `stat:lo:hi` entries, comma-separated, lo/hi each optional
        (blank = unbounded)."""
        raw = str(self.rules.get("modifier_stat_caps", ""))
        if not raw:
            return value
        for tok in raw.split(","):
            tok = tok.strip()
            if not tok:
                continue
            parts = tok.split(":")
            if len(parts) < 2 or parts[0].strip() != str(stat):
                continue
            try:
                lo = float(parts[1]) if parts[1].strip() else None
                hi = float(parts[2]) if len(parts) > 2 and parts[2].strip() else None
            except ValueError:
                continue
            if lo is not None and value < lo:
                value = lo
            if hi is not None and value > hi:
                value = hi
            return value
        return value

    # ---------- team-level state (resources + team modifiers) ----------
    def team_get(self, team: str, path: str, default: Any = None) -> Any:
        """Read a dotted path in a team's data dict (command points, morale,
        a `modifiers` bundle, ...). `default` if absent."""
        cur: Any = self.team_data.get(str(team))
        if not isinstance(cur, dict):
            return default
        for seg in str(path).split("."):
            if isinstance(cur, dict) and seg in cur:
                cur = cur[seg]
            else:
                return default
        return cur

    def team_has(self, team: str, path: str) -> bool:
        _sentinel = object()
        return self.team_get(team, path, _sentinel) is not _sentinel

    def team_set(self, team: str, path: str, value: Any) -> None:
        """Set a dotted path in a team's data dict (creating it + nested
        dicts as needed)."""
        cur = self.team_data.setdefault(str(team), {})
        segs = str(path).split(".")
        for seg in segs[:-1]:
            nxt = cur.get(seg)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[seg] = nxt
            cur = nxt
        cur[segs[-1]] = value

    def team_add(self, team: str, path: str, delta: float) -> Any:
        """Add `delta` to a numeric team value (0 if absent). Returns the new
        value."""
        cur = self.team_get(team, path, 0) or 0
        try:
            new = cur + delta
        except TypeError:
            raise VTTError(f"team_add: `{team}`.{path} is not numeric.")
        self.team_set(team, path, new)
        return new

    # ---------- condition-watchers (edge-triggered) ----------
    def fire_watchers(self) -> List[str]:
        """Re-check every watcher; fire its effect on a false->true edge
        (the condition newly becoming true). Single pass per call — all edges
        are recorded BEFORE any effect runs, so one effect can't perturb
        another watcher's reading this pass (a change it makes is caught on
        the next call). A `once` watcher is removed after firing. A malformed
        condition reads as not-met (fail-safe). Returns effect-error notes."""
        if not self.watchers:
            return []
        from formula import FormulaEngine, EvalCtx, FormulaError
        this_id = self.current_entity_id()
        engine = FormulaEngine(self)
        fired: List[str] = []
        for name, w in list(self.watchers.items()):
            cond = str(w.get("condition", "") or "")
            now = False
            if cond:
                try:
                    now = bool(engine.eval_expression(cond, EvalCtx(this=this_id)))
                except FormulaError:
                    now = False
            was = bool(w.get("last", False))
            w["last"] = now
            if now and not was:
                fired.append(name)
        log: List[str] = []
        for name in fired:
            w = self.watchers.get(name)
            if w is None:
                continue
            eff = str(w.get("effect", "") or "")
            if eff:
                try:
                    FormulaEngine(self).eval_program(
                        eff, EvalCtx(this=self.current_entity_id()))
                except FormulaError as ex:
                    log.append(f"⚠ watcher `{name}` effect error: {ex}")
            self.log_event("watcher_fired", name=name)
            if w.get("once"):
                self.watchers.pop(name, None)
        return log

    # ---- match outcome / victory (100) -------------------------------
    # No built-in objective evaluator: a win CONDITION is composed by the
    # GM from existing systems (a watcher whose effect calls declare_winner,
    # an action, an on_death passive, ...). The engine only stores the
    # declared outcome and exposes it — "victory is declared manually" by
    # default. declare_winner is also a formula primitive.

    def declare_winner(self, winner: str, reason: str = "") -> Dict[str, Any]:
        """Record a match outcome. `winner` is a free-form string (a team,
        an entity id, 'draw', whatever the GM means); `reason` is optional
        flavor. Overwrites any prior outcome. Returns the outcome dict."""
        self.outcome = {
            "winner": str(winner),
            "reason": str(reason or ""),
            "round": int(self.round_number),
        }
        self.log_event("winner_declared",
                       winner=self.outcome["winner"],
                       reason=self.outcome["reason"])
        return self.outcome

    def clear_outcome(self) -> bool:
        """Drop any declared outcome (resume play). Returns True if one was
        set."""
        had = self.outcome is not None
        self.outcome = None
        return had

    def _release_anchored_zones(self, eid: str) -> None:
        """Handle auras anchored to `eid` when it dies / is destroyed / leaves
        the match, per the anchored_zone_on_anchor_loss rule: 'delete' drops
        the zone, 'freeze' clears the binding and leaves the cells as a static
        zone, 'suspend' clears the cells (inert) but KEEPS the binding so the
        aura resumes (re-stamps) if the anchor is revived/healed — see
        _resume_anchored_zones."""
        mode = str(self.rules.get("anchored_zone_on_anchor_loss", "delete"))
        for name in [n for n, z in self.zones.items()
                     if isinstance(z, dict) and z.get("anchor") == eid]:
            z = self.zones[name]
            if mode == "freeze":
                z.pop("anchor", None)
                z.pop("anchor_radius", None)
                z.pop("anchor_metric", None)
            elif mode == "suspend":
                # Inert but still bound: empty cells (no render/hooks/
                # membership) while the anchor is dead; _restamp re-fills it
                # once the anchor is alive again (revive / part heal).
                z["cells"] = set()
            else:
                del self.zones[name]

    def _resume_anchored_zones(self, eid: str) -> None:
        """Re-stamp any auras still bound to `eid` after it comes back alive
        (corpse revive / part heal). A no-op unless the aura was suspended
        (mode 'suspend' kept the binding); _stamp_anchored_zone only re-fills
        when the anchor is actually alive, so calling this on a healthy
        anchor is harmless."""
        self._restamp_anchors_for(eid)

    def anchor_zone(self, name: str, eid: str, radius: int = 0,
                    metric: str = "square_radius") -> int:
        """Bind zone `name` to entity `eid` as an aura of `radius`, then
        stamp its cells immediately. Returns the resulting cell count.
        Raises NotFound for an unknown zone or entity, VTTError on a bad
        radius/metric. Re-anchoring an already-anchored zone just rebinds."""
        z = self._require_zone(name)
        if eid not in self.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        if not isinstance(radius, int) or isinstance(radius, bool) or radius < 0:
            raise VTTError("anchor: radius must be a non-negative integer.")
        metric = str(metric)
        if metric not in self._AURA_METRICS:
            raise VTTError(
                f"anchor: unknown metric '{metric}'. Use one of: "
                f"{', '.join(self._AURA_METRICS)}.")
        z["anchor"] = eid
        z["anchor_radius"] = radius
        z["anchor_metric"] = metric
        self._stamp_anchored_zone(name)
        return len(z["cells"])

    def unanchor_zone(self, name: str) -> bool:
        """Detach an aura's anchor, leaving its current cells as a static
        zone. Returns True iff the zone was anchored. Raises for an
        unknown zone."""
        z = self._require_zone(name)
        was = bool(z.get("anchor"))
        z.pop("anchor", None)
        z.pop("anchor_radius", None)
        z.pop("anchor_metric", None)
        return was

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
        """Names of every zone the entity currently stands in — any zone
        overlapping ANY of its footprint cells (a large entity is 'in' a
        zone if any part of its body is)."""
        e = self.entities.get(eid)
        if e is None:
            return set()
        return self._zones_over(self.entity_cells(e))

    def entities_in_zone(self, name: str) -> List[str]:
        """Ids of every alive entity with ANY footprint cell in the zone,
        in insertion order. [] for a missing zone."""
        z = self.zones.get(name)
        if z is None:
            return []
        cells = z["cells"]
        return [
            eid for eid, e in self.entities.items()
            if getattr(e, "is_alive", True) and not e.is_glued_part
            and any(c in cells for c in self.entity_cells(e))
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
                     label: Optional[str] = None,
                     pov: Optional[str] = None) -> bool:
        """Bind a channel to this match. Returns True if newly bound,
        False if it was already bound (label/pov still updated). The
        caller (command layer) keeps MatchManager's active_by_channel
        pointer in sync.

        `pov` sets the channel's point-of-view team for visibility
        filtering: a team string restricts the channel to that team's
        view; the literal "omniscient" (or "") CLEARS the POV back to
        the omniscient default (sees everything). None leaves any
        existing POV untouched (so a plain re-bind doesn't reset it)."""
        if not isinstance(channel_key, str) or not channel_key:
            raise VTTError("channel key must be a non-empty string.")
        newly = channel_key not in self.bound_channels
        meta = self.bound_channels.get(channel_key, {})
        if label is not None:
            meta["label"] = label
        if pov is not None:
            if pov == "" or pov == "omniscient":
                meta.pop("pov", None)
            else:
                meta["pov"] = pov
        self.bound_channels[channel_key] = meta
        return newly

    def channel_pov(self, channel_key: str) -> Optional[str]:
        """The point-of-view team for a channel, or None for an
        omniscient view (channel unbound, no POV set, or POV explicitly
        'omniscient'). None is the signal to render/list everything
        without consulting the visibility formula."""
        meta = self.bound_channels.get(channel_key)
        if not isinstance(meta, dict):
            return None
        pov = meta.get("pov")
        if not pov or pov == "omniscient":
            return None
        return str(pov)

    # ---- fog of war: range-only spatial vision -----------------------
    def _vision_radius_of(self, e: "Entity") -> int:
        """The entity's fog vision radius (the fog_vision_radius_var var),
        clamped to >= 0. Missing / non-integer -> 0 (sees only its own
        cell). Set a default with `!defvar add fog_vision_radius <n>`."""
        var = str(self.rules.get("fog_vision_radius_var", "fog_vision_radius"))
        try:
            return max(0, int(e.vars.get(var)))
        except (TypeError, ValueError):
            return 0

    def _within_vision(self, ux: int, uy: int, x: int, y: int,
                       radius: int) -> bool:
        """Is (x, y) within `radius` of (ux, uy) under fog_range_mode?"""
        dx, dy = abs(ux - x), abs(uy - y)
        mode = str(self.rules.get("fog_range_mode", "square_radius"))
        if mode == "manhattan":
            return dx + dy <= radius
        if mode == "euclidean":
            return dx * dx + dy * dy <= radius * radius
        # square_radius (Chebyshev) — the default
        return max(dx, dy) <= radius

    def team_sees_cell(self, pov_team: Optional[str], x: int, y: int) -> bool:
        """Does team `pov_team` see cell (x, y) by RANGE (any alive member
        within its vision radius — union team vision)? Omniscient / empty
        pov_team sees all. Does NOT consult fog_enabled or LOS; it's the raw
        range query. LOS-aware team vision is _team_sees(..., los=True)."""
        return self._team_sees(pov_team, x, y, los=False)

    def _team_sees_entity(self, pov_team: Optional[str], eid: str,
                          *, los: bool) -> bool:
        """Whether team `pov_team` sees entity `eid` — true when ANY cell
        of its footprint is seen (range, plus LOS when `los`). A large
        body is spotted if any part of it is in view."""
        e = self.entities.get(eid)
        if e is None:
            return False
        return any(self._team_sees(pov_team, cx, cy, los=los)
                   for cx, cy in self.entity_cells(e))

    def _team_has_los_entity(self, pov_team: Optional[str], eid: str) -> bool:
        """Any alive member of `pov_team` has a clear line to ANY of
        `eid`'s footprint cells (LOS only, ignores range)."""
        e = self.entities.get(eid)
        if e is None:
            return False
        return any(self._team_has_los(pov_team, cx, cy)
                   for cx, cy in self.entity_cells(e))

    def team_sees_entity(self, pov_team: Optional[str], eid: str) -> bool:
        """RANGE team vision of entity `eid` (any footprint cell). False
        for unknown."""
        return self._team_sees_entity(pov_team, eid, los=False)

    def entity_can_see(self, viewer_eid: str, x: int, y: int) -> bool:
        """Does the single unit `viewer_eid` see (x, y) by RANGE (within its
        own vision radius, any team)? False for unknown viewer."""
        return self._entity_sees(viewer_eid, x, y, los=False)

    # ---- opacity (line-of-sight blocking) ---------------------------
    # Opacity mirrors the movement-block system: a per-cell condition,
    # resolved instance `opaque` data > template `opaque` > the
    # tile_opaque_condition rule (zones: zone `opaque` data >
    # zone_opaque_condition). Evaluated as a formula with `self` = the
    # VIEWER (+ tile_x/tile_y), so it can be viewer-conditional ("opaque
    # unless entity[self].true_sight"). A bare bool/number is taken
    # directly. Fail-TRANSPARENT: a malformed / missing-var formula reads
    # as NOT opaque (a typo must not blind the table). SEPARATE from
    # `block` — a window is transparent+impassable, smoke opaque+passable.

    def _tile_opaque_spec(self, x: int, y: int) -> Any:
        """Opacity spec for the tile at (x, y): instance `opaque` data, else
        its template's, else the tile_opaque_condition rule."""
        cell = self.tiles.get((x, y))
        if cell is not None:
            inst = cell.get("opaque")
            if inst not in (None, ""):
                return inst
            tpl_name = cell.get("_template")
            if isinstance(tpl_name, str):
                tpl = self.tile_templates.get(tpl_name)
                if tpl is not None:
                    t_op = tpl.data.get("opaque")
                    if t_op not in (None, ""):
                        return t_op
        return self.rules.get("tile_opaque_condition", "")

    def _eval_opaque_spec(self, spec: Any, viewer_id: Optional[str],
                          extras: Dict[str, Any]) -> bool:
        """Evaluate an opacity spec with `self`=the viewer. bool/number used
        directly; a string runs as a formula. Empty/None or a malformed
        formula -> NOT opaque (transparent; fail toward visible)."""
        if spec is None:
            return False
        if isinstance(spec, bool):
            return spec
        if isinstance(spec, (int, float)):
            return bool(spec)
        cond = str(spec).strip()
        if not cond:
            return False
        from formula import FormulaEngine, EvalCtx, FormulaError
        engine = FormulaEngine(self)
        ctx = EvalCtx(this=self.current_entity_id(), target=viewer_id,
                      extras=dict(extras))
        try:
            return bool(engine.eval_expression(cond, ctx))
        except FormulaError:
            return False

    def cell_opaque(self, viewer_id: Optional[str], x: int, y: int) -> bool:
        """True iff the tile or any zone covering (x, y) blocks the sight of
        `viewer_id`. The raw opacity query (ignores fog toggles)."""
        if self._eval_opaque_spec(self._tile_opaque_spec(x, y), viewer_id,
                                  {"tile_x": x, "tile_y": y}):
            return True
        for name, z in self.zones.items():
            cells = z.get("cells", ())
            if (x, y) not in cells:
                continue
            zspec = (z.get("data") or {}).get("opaque")
            if zspec in (None, ""):
                zspec = self.rules.get("zone_opaque_condition", "")
            if self._eval_opaque_spec(
                    zspec, viewer_id,
                    {"zone_name": name, "tile_x": x, "tile_y": y}):
                return True
        return False

    def _los_stop(self, viewer_id: Optional[str], x1: int, y1: int,
                  x2: int, y2: int) -> Tuple[Tuple[int, int], bool, Optional[Tuple[int, int]]]:
        """Walk the thin LOS line (x1,y1)->(x2,y2) near->far with the SAME
        corner-mode flanker logic as sight, tracking where the line stops.
        Returns (last_clear, blocked, blocker):
          last_clear -- the farthest on-path cell reached with clear sight (the
                        beam's impact point; == (x2,y2) when the line is clear);
          blocked    -- True iff terrain stops the line before the target;
          blocker    -- the ON-PATH opaque cell that stopped it, or None when
                        the stop was an off-path corner-X (then last_clear is
                        the pre-corner cell).
        The single source of truth behind has_los / first_opaque / raycast so
        all three agree on los_corner_mode (permissive: only an X of BOTH
        flanking cells blocks; strict: any corner-touch; open: corners never
        block). Geometric integer DDA (cross-multiplied (2n+1) compare) so it
        is symmetric; the viewer's own cell and the target's own opacity never
        block."""
        if (x1, y1) == (x2, y2):
            return ((x2, y2), False, None)
        mode = str(self.rules.get("los_corner_mode", "permissive"))
        dx, dy = x2 - x1, y2 - y1
        sx = 1 if dx > 0 else (-1 if dx < 0 else 0)
        sy = 1 if dy > 0 else (-1 if dy < 0 else 0)
        adx, ady = abs(dx), abs(dy)
        cx, cy = x1, y1
        last = (x1, y1)
        nx = ny = 0  # boundary crossings consumed per axis
        guard = adx + ady + 2
        while guard > 0:
            guard -= 1
            if adx == 0:
                cy += sy
            elif ady == 0:
                cx += sx
            else:
                # tMaxX=(2nx+1)/(2adx) vs tMaxY=(2ny+1)/(2ady), cross-
                # multiplied to integers (no float drift at corners).
                a = (2 * nx + 1) * ady
                b = (2 * ny + 1) * adx
                if a == b:
                    # Corner: the segment crosses the point shared by four
                    # cells, passing diagonally. It only TOUCHES the two
                    # off-axis flankers (cx+sx,cy) and (cx,cy+sy).
                    f1 = (cx + sx, cy)
                    f2 = (cx, cy + sy)
                    if mode == "strict":
                        if self.cell_opaque(viewer_id, *f1) or \
                           self.cell_opaque(viewer_id, *f2):
                            return (last, True, None)
                    elif mode != "open":  # permissive (default)
                        if self.cell_opaque(viewer_id, *f1) and \
                           self.cell_opaque(viewer_id, *f2):
                            return (last, True, None)
                    cx += sx; cy += sy; nx += 1; ny += 1
                elif a < b:
                    cx += sx; nx += 1
                else:
                    cy += sy; ny += 1
            if (cx, cy) == (x2, y2):
                return ((x2, y2), False, None)
            if self.cell_opaque(viewer_id, cx, cy):
                return (last, True, (cx, cy))
            last = (cx, cy)
        return ((x2, y2), False, None)

    def has_los(self, viewer_id: Optional[str], x1: int, y1: int,
                x2: int, y2: int) -> bool:
        """Clear line of sight between the centers of (x1,y1) and (x2,y2)
        for `viewer_id`? Blocked by an opaque cell strictly between the
        endpoints; diagonal corner crossings obey los_corner_mode
        (permissive: only an X of BOTH flanking cells blocks; strict: any
        corner-touch blocks; open: corners never block). Symmetric. The
        viewer's own cell and the target's own opacity never block. viewer_id
        None -> viewer-conditional opacity reads transparent. Thin wrapper over
        the shared _los_stop walk so sight / first_opaque / raycast never
        drift on the corner rule."""
        return not self._los_stop(viewer_id, x1, y1, x2, y2)[1]

    def _line_cells(self, x1: int, y1: int, x2: int, y2: int) -> List[Tuple[int, int]]:
        """The ordered cells the segment (x1,y1)->(x2,y2) passes through,
        near->far, INCLUSIVE of both endpoints — the same thin line the LOS
        walk uses (a diagonal step at an exact corner). Geometry only, no
        opacity. The shared path behind first_opaque / entities_on_los /
        entities_in_line_ignorelos."""
        cells = [(x1, y1)]
        if (x1, y1) == (x2, y2):
            return cells
        dx, dy = x2 - x1, y2 - y1
        sx = 1 if dx > 0 else (-1 if dx < 0 else 0)
        sy = 1 if dy > 0 else (-1 if dy < 0 else 0)
        adx, ady = abs(dx), abs(dy)
        cx, cy = x1, y1
        nx = ny = 0
        guard = adx + ady + 2
        while (cx, cy) != (x2, y2) and guard > 0:
            guard -= 1
            if adx == 0:
                cy += sy
            elif ady == 0:
                cx += sx
            else:
                a = (2 * nx + 1) * ady
                b = (2 * ny + 1) * adx
                if a == b:
                    cx += sx; cy += sy; nx += 1; ny += 1
                elif a < b:
                    cx += sx; nx += 1
                else:
                    cy += sy; ny += 1
            cells.append((cx, cy))
        return cells

    def first_opaque(self, viewer_id: Optional[str], x1: int, y1: int,
                     x2: int, y2: int) -> Optional[Tuple[int, int]]:
        """The cell where sight stops between (x1,y1) and (x2,y2) for
        `viewer_id`, near->far, or None if the line is clear of terrain. For an
        ON-PATH opaque cell that's the opaque cell itself (the cell a beam
        strikes); for an off-path corner-X block (per los_corner_mode) it's the
        pre-corner cell where the line stops (the blocker is the diagonal
        flankers, not on the path). Corner-aware via the shared _los_stop walk,
        so it AGREES with has_los: clear iff has_los is clear."""
        last, blocked, blocker = self._los_stop(viewer_id, x1, y1, x2, y2)
        if not blocked:
            return None
        return blocker if blocker is not None else last

    def raycast(self, viewer_id: Optional[str], x1: int, y1: int,
                x2: int, y2: int) -> Tuple[int, int]:
        """The IMPACT point of a beam cast from (x1,y1) toward (x2,y2) for
        `viewer_id`: the farthest cell with clear sight before terrain stops
        it, or (x2,y2) if the whole line is clear. The companion to
        first_opaque (which returns the blocker); raycast returns where the
        beam LANDS. Corner-aware via the shared _los_stop walk (stops at the
        pre-corner cell on an off-path corner-X block), so it agrees with
        has_los. The viewer's own cell never blocks; the target's own opacity
        never blocks. If the cell adjacent to the origin is opaque the impact
        is the origin itself."""
        last, _blocked, _blocker = self._los_stop(viewer_id, x1, y1, x2, y2)
        return last

    # ---- combined vision (range and/or LOS) -------------------------
    def _member_sees(self, e: "Entity", x: int, y: int, *, los: bool) -> bool:
        # A large viewer sees from its WHOLE body: (x,y) is seen if any
        # footprint cell is within vision radius (and — under LOS — has a
        # clear line from that cell). The radius is measured per cell, so
        # a 3×3 scout projects its sight disc from every cell it covers.
        r = self._vision_radius_of(e)
        for (ex, ey) in self.entity_cells(e):
            if self._within_vision(ex, ey, x, y, r) and (
                    (not los) or self.has_los(e.id, ex, ey, x, y)):
                return True
        return False

    def _vision_member_ok(self, e: "Entity", pov_team: str) -> bool:
        """Whether `e` counts toward `pov_team`'s aggregate vision: an alive
        team member that isn't a glued part and (unless
        hidden_rider_grants_vision) isn't a hidden rider tucked inside a
        vehicle. Shared by _team_sees / _team_has_los / _record_vision so the
        'who provides sight' rule is defined once."""
        if not (e.is_alive and not e.is_glued_part and e.team == pov_team):
            return False
        if e.is_hidden_rider and not bool(
                self.rules.get("hidden_rider_grants_vision", False)):
            return False
        return True

    def _team_sees(self, pov_team: Optional[str], x: int, y: int,
                   *, los: bool) -> bool:
        if not pov_team:
            return True
        for e in self.entities.values():
            if self._vision_member_ok(e, pov_team) and \
               self._member_sees(e, x, y, los=los):
                return True
        return False

    def _team_has_los(self, pov_team: Optional[str], x: int, y: int) -> bool:
        """Any alive member of `pov_team` has a clear line to (x,y),
        IGNORING range (the losonly team query)."""
        if not pov_team:
            return True
        for e in self.entities.values():
            if self._vision_member_ok(e, pov_team) and \
               self._entity_has_los(e.id, x, y):
                return True
        return False

    def _entity_sees(self, eid: str, x: int, y: int, *, los: bool) -> bool:
        e = self.entities.get(eid)
        if e is None:
            return False
        return self._member_sees(e, x, y, los=los)

    def _entity_has_los(self, eid: str, x: int, y: int) -> bool:
        # A large viewer casts LOS from its WHOLE body — clear from ANY
        # footprint cell counts (mirrors _member_sees, which checks every
        # cell for the range+LOS query).
        e = self.entities.get(eid)
        if e is None:
            return False
        return any(self.has_los(eid, ex, ey, x, y)
                   for (ex, ey) in self.entity_cells(e))

    def _fog_team_sees(self, pov_team: Optional[str], x: int, y: int) -> bool:
        """Team vision for the FOG feature: range, plus LOS when the fog_los
        rule is on. The single switch behind fog hiding + explored memory.

        Memoized when `_vision_memo` is active (set around a read-only
        operation that scans many cells — see render_ascii). The memo is NEVER
        held across a mutation: it lives only for the duration of one
        synchronous read pass, so it can't go stale. A full-map render
        otherwise recomputes the same (team, cell) sight — looping every team
        member with a per-member LOS walk — once per layer per cell."""
        memo = self._vision_memo
        if memo is None:
            return self._team_sees(
                pov_team, x, y, los=bool(self.rules.get("fog_los", False)))
        key = (pov_team, x, y)
        cached = memo.get(key)
        if cached is None:
            cached = self._team_sees(
                pov_team, x, y, los=bool(self.rules.get("fog_los", False)))
            memo[key] = cached
        return cached

    def _cell_remembered(self, pov_team: Optional[str], x: int, y: int) -> bool:
        """True iff fog memory is on and `pov_team` has explored (x, y)."""
        if not self.fog_memory or not pov_team:
            return False
        return (x, y) in self.explored.get(pov_team, ())

    # ---- fog reveals (scout / clairvoyance) ----
    def _active_reveals(self, team: str) -> "list":
        """The team's non-expired reveal records, pruning expired ones in
        place (lazy cleanup keyed off round_number)."""
        recs = self.fog_reveals.get(team)
        if not recs:
            return []
        live = [r for r in recs
                if r.get("until") is None or int(r["until"]) >= self.round_number]
        if len(live) != len(recs):
            if live:
                self.fog_reveals[team] = live
            else:
                self.fog_reveals.pop(team, None)
        return live

    def _cell_revealed(self, pov_team: Optional[str], x: int, y: int) -> bool:
        """True iff `pov_team` has an active reveal covering (x, y)."""
        if not pov_team:
            return False
        for r in self._active_reveals(pov_team):
            if (x, y) in r.get("cells", ()):
                return True
        return False

    def reveal_cells(self, team: str, cells, duration: Optional[int] = None) -> int:
        """Reveal a set of cells to `team` for `duration` rounds (None =
        permanent), independent of unit vision. Returns the cell count.
        Clipped to the grid."""
        clipped = {(int(x), int(y)) for (x, y) in cells
                   if self.in_bounds(int(x), int(y))}
        if not clipped:
            return 0
        until = None if duration is None else self.round_number + int(duration)
        self.fog_reveals.setdefault(team, []).append(
            {"cells": clipped, "until": until})
        return len(clipped)

    def clear_reveals(self, team: str) -> int:
        """Drop all reveals for `team`. Returns the number of records removed."""
        return len(self.fog_reveals.pop(team, []) or [])

    def _fog_terrain_visible(self, pov_team: Optional[str], x: int, y: int) -> bool:
        """Fog gate for STATIC features (tiles, zones, corpses) and the map
        overlay: visible when fog is off, omniscient, currently seen (range
        + LOS per fog_los), OR (memory on) explored."""
        if not self.fog_enabled or pov_team is None:
            return True
        return (self._fog_team_sees(pov_team, x, y)
                or self._cell_remembered(pov_team, x, y)
                or self._cell_revealed(pov_team, x, y))

    def _fog_entity_visible(self, pov_team: Optional[str], x: int, y: int) -> bool:
        """Fog gate for LIVE entities: visible on current vision (range +
        LOS per fog_los); on a remembered cell only when fog_memory_mode is
        'full'."""
        if not self.fog_enabled or pov_team is None:
            return True
        if self._fog_team_sees(pov_team, x, y):
            return True
        if self._cell_revealed(pov_team, x, y):
            return True  # a reveal shows live entities too (clairvoyance)
        if self._cell_remembered(pov_team, x, y):
            return str(self.rules.get("fog_memory_mode", "full")) == "full"
        return False

    def _record_vision(self, team: Optional[str]) -> None:
        """Fold `team`'s CURRENT vision into its explored set. No-op unless
        fog AND fog memory are on. Iterates each alive member's vision-radius
        NEIGHBOURHOOD (not the whole grid) and, when fog_los is on, requires
        a clear line — so explored grows 'as cells are revealed'."""
        if not self.fog_enabled or not self.fog_memory or not team:
            return
        seen = self.explored.setdefault(team, set())
        use_los = bool(self.rules.get("fog_los", False))
        for e in self.entities.values():
            if not self._vision_member_ok(e, team):
                continue
            r = self._vision_radius_of(e)
            # A large viewer reveals the UNION of every footprint cell's
            # vision-radius neighbourhood (LOS measured from that cell).
            for (ex, ey) in self.entity_cells(e):
                for yy in range(max(1, ey - r), min(self.grid_height, ey + r) + 1):
                    for xx in range(max(1, ex - r), min(self.grid_width, ex + r) + 1):
                        if (xx, yy) in seen:
                            continue
                        if not self._within_vision(ex, ey, xx, yy, r):
                            continue
                        if use_los and not self.has_los(e.id, ex, ey, xx, yy):
                            continue
                        seen.add((xx, yy))

    def _visibility_visible(self, rule_key: str, pov_team: Optional[str], *,
                            target: Optional[str] = None,
                            extras: Optional[Dict[str, Any]] = None) -> bool:
        """Shared evaluator behind the *_visibility_condition rules
        (entity / tile / zone / corpse). An omniscient viewer (pov_team
        is None) sees everything; an empty rule means visible; a
        malformed formula is treated as visible (reveal rather than blank
        the board on a GM typo — same stance as the death-condition
        evaluator). `extras` supplies the per-kind bindings; pov_team is
        added automatically. `target` binds `self` (only entities have
        one; tiles/zones/corpses pass None)."""
        if pov_team is None:
            return True
        cond = str(self.rules.get(rule_key, "")).strip()
        if not cond:
            return True
        ex = dict(extras or {})
        ex["pov_team"] = pov_team
        from formula import FormulaEngine, EvalCtx, FormulaError
        engine = FormulaEngine(self)
        ctx = EvalCtx(this=self.current_entity_id(), target=target, extras=ex)
        try:
            return bool(engine.eval_expression(cond, ctx))
        except FormulaError:
            return True

    def entity_visible_to(self, eid: str, pov_team: Optional[str]) -> bool:
        """Whether entity `eid` is visible to a viewer whose POV is
        `pov_team` (None = omniscient = always visible). Evaluates
        entity_visibility_condition with `self`=the entity and `pov_team`
        bound; empty rule / malformed = visible."""
        if pov_team is None:
            return True
        if eid not in self.entities:
            return False
        e = self.entities[eid]
        # A rider tucked inside a vehicle (hidden slot) isn't independently
        # visible under a POV — you see the vehicle, not who's inside. A
        # visible (region-slot) rider stays subject to the normal checks.
        if e.is_hidden_rider:
            return False
        # A large entity is fog-visible if ANY footprint cell passes the
        # entity fog gate (current vision, or remembered when memory mode
        # is 'full') — so a giant is hidden only when its whole body is
        # in fog.
        if not any(self._fog_entity_visible(pov_team, cx, cy)
                   for cx, cy in self.entity_cells(e)):
            return False
        return self._visibility_visible(
            "entity_visibility_condition", pov_team, target=eid)

    def tile_visible_to(self, x: int, y: int, pov_team: Optional[str]) -> bool:
        """Whether the tile at (x, y) — its map glyph and its !tile
        list/info/cells rows — is visible to `pov_team`. Binds tile_x /
        tile_y (inspect data via tile_get/tile_has). See
        tile_visibility_condition."""
        if pov_team is not None and not self._fog_terrain_visible(pov_team, x, y):
            return False
        return self._visibility_visible(
            "tile_visibility_condition", pov_team,
            extras={"tile_x": x, "tile_y": y})

    def zone_visible_to(self, name: str, pov_team: Optional[str]) -> bool:
        """Whether zone `name` — its map glyph and its !zone listing row
        — is visible to `pov_team`. Binds zone_name (inspect via
        zone_get/zone_has). See zone_visibility_condition. Under fog a
        zone is revealed if the team sees ANY of its cells (the seen part
        shows; the fog overlay still covers its unseen cells on the map)."""
        if pov_team is not None and self.fog_enabled:
            z = self.zones.get(name)
            cells = z.get("cells", ()) if isinstance(z, dict) else ()
            if cells and not any(
                self._fog_terrain_visible(pov_team, cx, cy) for (cx, cy) in cells
            ):
                return False
        return self._visibility_visible(
            "zone_visibility_condition", pov_team,
            extras={"zone_name": name})

    def _corpse_footprint(self, corpse: Dict[str, Any]) -> Tuple[int, int]:
        """(w, h) of a corpse's footprint, read from the dead entity's
        stored footprint vars (1×1 when absent) — so a large entity
        leaves a large corpse. One stored corpse identity, footprint
        derived, exactly like the live-entity convention."""
        w = h = 1
        ent = corpse.get("entity") if isinstance(corpse, dict) else None
        cv = ent.get("vars", {}) if isinstance(ent, dict) else {}
        if isinstance(cv, dict):
            try:
                w = max(1, int(cv.get(str(self.rules.get("footprint_width_var", "footprint_w")))))
            except (TypeError, ValueError):
                w = 1
            try:
                h = max(1, int(cv.get(str(self.rules.get("footprint_height_var", "footprint_h")))))
            except (TypeError, ValueError):
                h = 1
        return w, h

    def corpse_cells(self, x: int, y: int,
                     corpse: Dict[str, Any]) -> List[Tuple[int, int]]:
        """The cells a corpse covers, anchored at its tile (x, y)."""
        w, h = self._corpse_footprint(corpse)
        return [(x + dx, y + dy) for dy in range(h) for dx in range(w)]

    def corpse_visible_to(self, eid: str, corpse: Dict[str, Any],
                          x: int, y: int, pov_team: Optional[str]) -> bool:
        """Whether a corpse (Dead: row) is visible to `pov_team`. A
        corpse is a stored snapshot, not a live entity, so the formula
        gets corpse_team (the dead entity's team_var value at death) +
        tile_x / tile_y rather than `self`. A large corpse is visible if
        ANY cell of its footprint is fog-visible. See
        corpse_visibility_condition."""
        if pov_team is None:
            return True
        if not any(self._fog_terrain_visible(pov_team, cx, cy)
                   for cx, cy in self.corpse_cells(x, y, corpse)):
            return False
        team_var = str(self.rules.get("team_var", "team"))
        corpse_team = ""
        ent = corpse.get("entity") if isinstance(corpse, dict) else None
        if isinstance(ent, dict):
            cv = ent.get("vars", {})
            if isinstance(cv, dict):
                corpse_team = str(cv.get(team_var, "") or "")
        return self._visibility_visible(
            "corpse_visibility_condition", pov_team,
            extras={"corpse_team": corpse_team, "tile_x": x, "tile_y": y})

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

    # ---- entity footprint (multi-tile / large entities) --------------
    # A large entity occupies a W×H rectangle of cells anchored at its
    # TOP-LEFT cell (entity.x / entity.y). W and H live in entity vars
    # named by the footprint_width_var / footprint_height_var rules,
    # defaulting to 1×1 when the var is absent or non-positive — so a
    # plain entity behaves exactly as before. Every spatial RELATION
    # (occupancy, movement validation, spawn/summon placement,
    # rendering, and — in later layers — vision / distance / AoE) works
    # in terms of the full footprint; the anchor is only the addressing
    # convention: what entity[X].x means and where a bare coord points.
    # The footprint extends RIGHT (+x) and DOWN (+y) from the anchor.

    def _footprint_dim(self, e: "Entity", rule_key: str, default_name: str) -> int:
        var = str(self.rules.get(rule_key, default_name))
        try:
            v = int(e.vars.get(var))
        except (TypeError, ValueError):
            return 1
        return v if v >= 1 else 1

    def entity_footprint(self, e: "Entity") -> Tuple[int, int]:
        """(width, height) of `e`'s footprint, each >= 1. 1×1 when the
        footprint vars are unset / non-positive (the ordinary case)."""
        return (self._footprint_dim(e, "footprint_width_var", "footprint_w"),
                self._footprint_dim(e, "footprint_height_var", "footprint_h"))

    def entity_cells(self, e: "Entity",
                     ax: Optional[int] = None,
                     ay: Optional[int] = None) -> List[Tuple[int, int]]:
        """The list of (x, y) cells `e`'s footprint covers, anchored at
        (ax, ay) — defaulting to the entity's current position. Top-left
        anchored, row-major order (so [0] is always the anchor cell)."""
        # A region part's cells are an explicit facing-derived set over the
        # parent's footprint, not a rectangle (only for a current-position
        # query — an ax/ay override means a movement sweep, which region
        # parts don't do).
        if ax is None and ay is None and e.is_region_part:
            return self.part_region_cells(e)
        if ax is None:
            ax = e.x
        if ay is None:
            ay = e.y
        w, h = self.entity_footprint(e)
        return [(ax + dx, ay + dy) for dy in range(h) for dx in range(w)]

    def entity_occupies(self, e: "Entity", x: int, y: int) -> bool:
        """True iff (x, y) lies within `e`'s footprint at its current
        anchor — the membership test behind cell_occupant."""
        w, h = self.entity_footprint(e)
        return e.x <= x < e.x + w and e.y <= y < e.y + h

    def _rect_gap(self, ax: int, ay: int, aw: int, ah: int,
                  bx: int, by: int, bw: int, bh: int,
                  mode: str = "square_radius") -> float:
        """Nearest-cell distance between two axis-aligned rectangles (each
        x,y = top-left, w,h = size), combined per `mode`. The per-axis gap
        is 0 when the rectangles overlap on that axis, else the separation;
        the chosen metric is then applied. Mirrors the point-distance
        metrics exactly (square_radius/Chebyshev int, manhattan int,
        euclidean float), so two 1×1 rects reduce to anchor distance."""
        gx = max(0, ax - (bx + bw - 1), bx - (ax + aw - 1))
        gy = max(0, ay - (by + bh - 1), by - (ay + ah - 1))
        if mode in ("manhattan", "manhattan_distance"):
            return int(gx + gy)
        if mode in ("euclidean", "euclidean_distance"):
            return math.sqrt(gx * gx + gy * gy)
        return int(max(gx, gy))   # square_radius (Chebyshev), the default

    def entity_gap_distance(self, e_ref: "Entity", e_other: "Entity",
                            mode: str = "square_radius") -> float:
        """Footprint-aware nearest-cell distance between two entities —
        the gap between their footprint rectangles. The single source of
        truth for entity-to-entity distance (entities_within/nearest_entity
        and the `near:` find predicate both route through here)."""
        rw, rh = self.entity_footprint(e_ref)
        ow, oh = self.entity_footprint(e_other)
        return self._rect_gap(e_ref.x, e_ref.y, rw, rh,
                              e_other.x, e_other.y, ow, oh, mode)

    def cell_entity_distance(self, x: int, y: int, e: "Entity",
                             mode: str = "square_radius") -> float:
        """Footprint-aware nearest-cell distance from a single cell (x, y)
        to entity `e` — the point-vs-footprint gap behind the `within:`
        find predicate."""
        ow, oh = self.entity_footprint(e)
        return self._rect_gap(x, y, 1, 1, e.x, e.y, ow, oh, mode)

    def cell_occupant(self, x: int, y: int,
                      ignore: Tuple[str, ...] = ()) -> Optional[str]:
        """The id of the first alive, non-stackable entity whose
        FOOTPRINT covers (x, y), skipping any id in `ignore`; None when
        the cell is free of blockers. The footprint-aware core behind
        is_occupied — a large entity blocks every cell it spans."""
        for eid, e in self.entities.items():
            if eid in ignore:
                continue
            # Glued body parts ride on the parent's cell and never block —
            # the parent is the occupant. A LOCATED part has its own cell
            # and occupies it normally.
            if e.is_glued_part:
                continue
            # Mounted riders are aboard the vehicle, not on the ground — they
            # never block (the vehicle's own footprint is the occupant). This
            # holds for visible riders too (a gunner shares the tank's cell).
            if e.is_mounted:
                continue
            if e.is_alive and not e.is_cell_stackable and self.entity_occupies(e, x, y):
                return eid
        return None

    def _occupancy_ignore(self, e: "Entity",
                          extra: Tuple[str, ...] = ()) -> Tuple[str, ...]:
        """Ids a moving entity ignores for occupancy: always itself, PLUS its
        own snake-body segments when `e` is a snake head with self-collision
        OFF (the Destroyer passes through its own body; a classic-Snake head
        is blocked by it). Other movers see segments as occupying normally."""
        ids: Tuple[str, ...] = (e.id,) + tuple(extra)
        if self.is_snake_head(e.id) and not bool(self._segment_cfg(
                e, "__segment_self_collision", "segment_self_collision")):
            ids += tuple(s.id for s in self.snake_segments(e.id))
        return ids

    def is_occupied(self, x: int, y: int, ignore_entity_id: Optional[str] = None) -> bool:
        """True when the cell (x, y) is covered by the footprint of an
        entity that BLOCKS others from entering — i.e. an alive
        non-stackable entity (a W×H entity blocks all W*H of its cells).
        Stackable entities (vars `__cell_stackable` truthy) don't count
        as occupying their cells; a tile holding only stackable
        residents still answers False here. Pair this with the
        mover-side check in Entity.tp / move_dirs / Match.summon_entity:
        those skip the occupancy call entirely when the MOVER itself is
        stackable, so a stackable entity can also enter a cell that has
        a non-stackable resident."""
        ignore = (ignore_entity_id,) if ignore_entity_id else ()
        return self.cell_occupant(x, y, ignore) is not None

    # ---- footprint-aware placement validation ------------------------
    # The single gate every validated placement (tp / move_dirs final /
    # spawn / summon / resize) funnels through, so footprint semantics
    # live in one place. The raw move_to primitive and direct position
    # writes stay single-cell and ungated, as before.

    def footprint_in_bounds(self, e: "Entity", ax: int, ay: int) -> bool:
        """True iff every cell of `e`'s footprint anchored at (ax, ay)
        is on the grid."""
        return all(self.in_bounds(cx, cy) for cx, cy in self.entity_cells(e, ax, ay))

    def _validate_placement(self, e: "Entity", ax: int, ay: int,
                            mode: Optional[str]) -> None:
        """Validate that `e` can occupy the footprint anchored at
        (ax, ay): every cell in bounds, none occupied by another entity
        (skipped when `e` is stackable), and — when `mode` is not None —
        none blocking movement of kind `mode` ('walk'/'tp'/'swap').
        Raises OutOfBounds / Occupied / Blocked naming the offending
        cell. `e`'s own footprint is always ignored for occupancy, so a
        large entity stepping forward doesn't collide with the cells it
        is vacating. Shared by tp / move_dirs / spawn / summon."""
        cells = self.entity_cells(e, ax, ay)
        for cx, cy in cells:
            if not self.in_bounds(cx, cy):
                raise OutOfBounds(
                    f"({cx},{cy}) outside {self.grid_width}x{self.grid_height}")
        # GLUED body parts ride on the parent's cell and never collide
        # (non-occupying), so they skip the occupancy gate like a stackable
        # entity. A LOCATED part (own cell) DOES get the occupancy check.
        # Keyed off the raw part_of / __part_located vars (not the is_*
        # properties) because spawn validates placement BEFORE binding the
        # entity to the match, when those can't see the parent yet.
        glued = bool(e.part_of) and not e.vars.get("__part_located")
        if not e.is_cell_stackable and not glued:
            ignore_ids = self._occupancy_ignore(e)
            for cx, cy in cells:
                if self.cell_occupant(cx, cy, ignore_ids) is not None:
                    raise Occupied(f"Cell ({cx},{cy}) already occupied")
        if mode is not None:
            for cx, cy in cells:
                if self._check_block(e.id, cx, cy, mode):
                    raise Blocked(f"Cell ({cx},{cy}) blocks `{e.id}`")

    # ---- movement blocking (impassable tiles / zones) ----------------
    # A cell blocks a mover when its tile OR any covering zone evaluates a
    # block condition truthy for that entity. Tile spec resolution mirrors
    # the glyph layering: instance `block` data > template `block` data >
    # the tile_block_condition rule. Zone: the zone's `block` data > the
    # zone_block_condition rule. A spec is a formula STRING (evaluated with
    # `self`=mover + tile_x/tile_y or zone_name) or a bare bool/number.

    def _tile_block_spec(self, x: int, y: int) -> Any:
        """The block spec governing the tile at (x, y): the instance's
        `block` data, else its template's `block`, else the
        tile_block_condition rule. Returns "" when nothing applies."""
        cell = self.tiles.get((x, y))
        if cell is not None:
            inst = cell.get("block")
            if inst not in (None, ""):
                return inst
            tpl_name = cell.get("_template")
            if isinstance(tpl_name, str):
                tpl = self.tile_templates.get(tpl_name)
                if tpl is not None:
                    t_block = tpl.data.get("block")
                    if t_block not in (None, ""):
                        return t_block
        return self.rules.get("tile_block_condition", "")

    def _eval_block_spec(self, spec: Any, mover_id: str,
                         extras: Dict[str, Any]) -> bool:
        """Evaluate a block spec for `mover_id`. bool/number specs are used
        directly; a string is run as a formula with `self`=the mover plus
        `extras` bindings. Empty/None = not blocking. A malformed formula
        FAILS OPEN (returns False) so a GM typo can't trap units in place —
        same fail-toward-permissive stance as the visibility rules."""
        if spec is None:
            return False
        if isinstance(spec, bool):
            return spec
        if isinstance(spec, (int, float)):
            return bool(spec)
        cond = str(spec).strip()
        if not cond:
            return False
        from formula import FormulaEngine, EvalCtx, FormulaError
        engine = FormulaEngine(self)
        ctx = EvalCtx(this=self.current_entity_id(), target=mover_id,
                      extras=dict(extras))
        try:
            return bool(engine.eval_expression(cond, ctx))
        except FormulaError:
            return False

    def cell_blocks(self, mover_id: str, x: int, y: int) -> bool:
        """True iff the tile or any zone covering (x, y) blocks `mover_id`
        from entering. The raw geometry query — does NOT consult the
        block_* movement-mode rules (use _check_block for that)."""
        if self._eval_block_spec(
                self._tile_block_spec(x, y), mover_id,
                {"tile_x": x, "tile_y": y}):
            return True
        for name, z in self.zones.items():
            cells = z.get("cells", ())
            if (x, y) not in cells:
                continue
            zspec = (z.get("data") or {}).get("block")
            if zspec in (None, ""):
                zspec = self.rules.get("zone_block_condition", "")
            if self._eval_block_spec(zspec, mover_id, {"zone_name": name}):
                return True
        # Corpses can block, per corpse_block_condition (off by default).
        # Gated on the rule being set so there's zero cost when corpses are
        # passable (the default). A large corpse blocks its whole footprint.
        cspec = self.rules.get("corpse_block_condition", "")
        if cspec not in (None, ""):
            team_var = str(self.rules.get("team_var", "team"))
            for cx, cy, cid, corpse in self.all_corpses():
                if (x, y) not in self.corpse_cells(cx, cy, corpse):
                    continue
                ct = ""
                ent = corpse.get("entity") if isinstance(corpse, dict) else None
                if isinstance(ent, dict):
                    cv = ent.get("vars", {})
                    if isinstance(cv, dict):
                        ct = str(cv.get(team_var, "") or "")
                if self._eval_block_spec(
                        cspec, mover_id,
                        {"tile_x": x, "tile_y": y,
                         "corpse_id": cid, "corpse_team": ct}):
                    return True
        return False

    def _check_block(self, mover_id: str, x: int, y: int, mode: str) -> bool:
        """Whether a movement of kind `mode` ('walk'/'tp'/'push'/'swap')
        onto (x, y) is blocked for `mover_id` — i.e. the matching block_<mode>
        rule is on AND the cell blocks the mover."""
        if not self.rules.get(f"block_{mode}", True):
            return False
        return self.cell_blocks(mover_id, x, y)

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
        # Mounts: redirect a driver's push to its VEHICLE (the whole rig is
        # shoved, riders carried); a passenger raises ("dismount first").
        # Resolve BEFORE the footprint prefix-walk so the vehicle's footprint
        # is the one validated AND committed — otherwise the walk OK's the
        # rider's 1x1 path and the redirected vehicle move_dirs then fails
        # mid-commit. Mirrors tp/move_dirs/swap.
        e = e._mount_move_redirect()
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
            # Footprint-aware: the whole shifted footprint must be in
            # bounds, unoccupied (by others), and unblocked — the push
            # stops at the cell before the first cell that fails for any
            # part of the body.
            if not self.footprint_in_bounds(e, nx, ny):
                break
            if not stackable and any(
                    self.cell_occupant(cx, cy, self._occupancy_ignore(e)) is not None
                    for cx, cy in self.entity_cells(e, nx, ny)):
                break
            # A blocked cell stops the push at the cell before it (knockback
            # into a wall) — same "stops at the first blocker" rule as
            # occupancy above.
            if any(self._check_block(e.id, cx, cy, "push")
                   for cx, cy in self.entity_cells(e, nx, ny)):
                break
            x, y = nx, ny
            steps += 1
        if steps == 0:
            return 0, []
        # Delegate the actual commit to move_dirs (we've already proven
        # the path legal) so we inherit per-step hook firing for free.
        # block_mode=None: we already applied the push-mode block check.
        log = e.move_dirs([(canon, steps)], block_mode=None)
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
        # Mounts: a driver's pull drags its VEHICLE (riders carried); a
        # passenger raises ("dismount first"). Resolve before the footprint
        # prefix-walk so the vehicle's body is validated AND committed (see
        # push_entity). Mirrors tp/move_dirs/swap.
        e = e._mount_move_redirect()
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
            # Footprint-aware (see push_entity): the whole shifted body
            # must be in bounds, clear of others, and unblocked.
            if not self.footprint_in_bounds(e, nx, ny):
                break
            if not stackable and any(
                    self.cell_occupant(cx, cy, self._occupancy_ignore(e)) is not None
                    for cx, cy in self.entity_cells(e, nx, ny)):
                break
            # Blocking stops the drag at the cell before a blocked one,
            # mirroring push_entity's "stops at the first blocker" rule.
            if any(self._check_block(e.id, cx, cy, "push")
                   for cx, cy in self.entity_cells(e, nx, ny)):
                break
            moves.append((_VECTOR_TO_DIRECTION[(sx, sy)], 1))
            x, y = nx, ny
        if not moves:
            return 0, []
        # block_mode=None — push-mode block already applied per step above.
        log = e.move_dirs(moves, block_mode=None)
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
        # Mount redirect (mirrors move/tp): a mounted entity can't be swapped
        # on its own. A driver (a slot with controls_movement) redirects the
        # swap to its VEHICLE — the whole rig swaps and riders are carried via
        # fire_entity_moved; a passenger raises ("dismount first"). Resolved
        # before reading positions so the swap operates on the real movers.
        ea = ea._mount_move_redirect()
        eb = eb._mount_move_redirect()
        a, b = ea.id, eb.id
        if a == b:
            return False, []
        ax, ay = ea.x, ea.y
        bx, by = eb.x, eb.y
        # Degenerate co-located case (shouldn't happen for two non-
        # stackable entities, but defended): nothing moves, no hooks.
        if (ax, ay) == (bx, by):
            return False, []
        # Each partner's ANCHOR lands on the other's anchor; its whole
        # footprint re-anchors there. For equal-size entities this is a
        # clean exchange; for DIFFERENT sizes it's only legal when each
        # relocated footprint fits — in bounds, and clear of every entity
        # except the two being swapped (they vacate each other). Checked
        # before any hook fires — all-or-nothing. Block (swap mode) is
        # checked per covered cell.
        pair = (a, b)
        for mover, mx, my in ((ea, bx, by), (eb, ax, ay)):
            for cx, cy in self.entity_cells(mover, mx, my):
                if not self.in_bounds(cx, cy):
                    raise OutOfBounds(
                        f"swap: ({cx},{cy}) outside "
                        f"{self.grid_width}x{self.grid_height} for `{mover.id}`")
            if not mover.is_cell_stackable:
                ig = self._occupancy_ignore(mover, extra=pair)
                for cx, cy in self.entity_cells(mover, mx, my):
                    if self.cell_occupant(cx, cy, ig) is not None:
                        raise Occupied(
                            f"swap: cell ({cx},{cy}) occupied — `{mover.id}` "
                            f"doesn't fit at the swap target")
            for cx, cy in self.entity_cells(mover, mx, my):
                if self._check_block(mover.id, cx, cy, "swap"):
                    raise Blocked(f"Cell ({cx},{cy}) blocks `{mover.id}`")
        log: List[str] = []
        # Footprint cell sets for each partner at its old and new anchor.
        a_old = self.entity_cells(ea, ax, ay)
        a_new = self.entity_cells(ea, bx, by)
        b_old = self.entity_cells(eb, bx, by)
        b_new = self.entity_cells(eb, ax, ay)
        # Fire exit-side hooks at CURRENT positions before any state change,
        # so passives observe each entity still standing on its old cells.
        log += self.fire_footprint_tile_exit(a, a_old, a_new)
        log += self.fire_footprint_zone_exit(a, a_old, a_new)
        log += self.fire_footprint_tile_exit(b, b_old, b_new)
        log += self.fire_footprint_zone_exit(b, b_old, b_new)
        # Atomic swap via direct move_to (bypasses is_occupied — required,
        # since A and B occupy each other's target cells until done).
        ea.move_to(bx, by)
        eb.move_to(ax, ay)
        # Each swap partner's move is a single, final step — enter + stop.
        log += self.fire_footprint_tile_enter(a, a_old, a_new)
        log += self.fire_footprint_tile_stop(a, a_new)
        log += self.fire_footprint_zone_enter(a, a_old, a_new, True)
        log += self.fire_footprint_tile_enter(b, b_old, b_new)
        log += self.fire_footprint_tile_stop(b, b_new)
        log += self.fire_footprint_zone_enter(b, b_old, b_new, True)
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

    def _attached_tick_parts(self, base_targets: List[str]) -> List[str]:
        """The attached parts that should share the status/turn-passive
        CLOCK of `base_targets`. A part carries no initiative of its own
        (unless made independent, i.e. placed in turn_order), so without
        this its statuses and its on_turn_*/on_round_* passives would never
        fire. BFS each base target's part subtree and collect the parts
        that LACK independent initiative, STOPPING descent at an independent
        part (it is its own target and ticks on its own turn, carrying its
        own sub-parts). Deduped against the base targets and each other, so
        a deep part isn't added twice and an independent part reached via
        both its own turn-order slot and its parent isn't double-counted.
        Returns the EXTRA part ids only (base targets excluded)."""
        turn_set = set(self.turn_order)
        seen = set(base_targets)
        extra: List[str] = []
        for t in list(base_targets):
            frontier = [c.id for c in self.entity_parts(t)]
            while frontier:
                pid = frontier.pop(0)
                if pid in seen:
                    continue
                seen.add(pid)
                if pid in turn_set:
                    # Independent part: its own target; don't descend.
                    continue
                extra.append(pid)
                frontier.extend(c.id for c in self.entity_parts(pid))
        return extra

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
        # Active Time Battle: rounds are disabled — selection is by charge
        # bar, not turn-order cycling. Whole separate path.
        if self.rules.get("atb_enabled"):
            return self._atb_next_turn()
        # Round-based play: clear the ATB warn latch so re-enabling ATB warns
        # about dormant round logic again.
        self._atb_round_warned = False
        log: List[str] = []
        # First-ever next_turn call: begin round 1 without advancing.
        if not self.round_started:
            self.round_started = True
            log.extend(self.fire_hook(
                "on_round_start",
                own_only_targets=self._attached_tick_parts(list(self.turn_order))))
            log.extend(self.fire_status_tick("round_start"))
            log.extend(self.fire_tile_time_hooks("on_round_start"))
            log.extend(self.fire_zone_time_hooks("on_round_start"))
            log.extend(self.fire_scheduled_round())
            # Autosave: round start happens after on_round_start hooks
            # so that restoring the snapshot gives the players the
            # state they would have seen as the round began (with any
            # hook side-effects already applied).
            self.history.record_round(self)
            # An opening round_start hook/tick may have removed every
            # entity — nothing to start.
            if not self.turn_order:
                self.history.record_turn(self)
                return (None, log)
            # The opening entity may itself be skippable (e.g. starts
            # stunned) — skip forward to the first eligible one.
            eligible = self._skip_to_eligible(log)
            # A skip's round-wrap (its internal _advance_index firing
            # on_round_end/start hooks) can itself empty the order — re-check
            # before indexing, same guard as after the turn_end/advance steps.
            if not self.turn_order:
                self.history.record_turn(self)
                return (None, log)
            cur = self.turn_order[self.active_index]
            if eligible:
                log.extend(self.fire_hook(
                    "on_turn_start", target_ids=[cur],
                    own_only_targets=self._attached_tick_parts([cur])))
                log.extend(self.fire_status_tick("turn_start"))
                log.extend(self.fire_tile_time_hooks("on_turn_start"))
                log.extend(self.fire_zone_time_hooks("on_turn_start"))
                log.extend(self.fire_scheduled_turn(cur))
                for pid in self._attached_tick_parts([cur]):
                    log.extend(self.fire_scheduled_turn(pid))
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
        log.extend(self.fire_hook(
            "on_turn_end", target_ids=[cur],
            own_only_targets=self._attached_tick_parts([cur])))
        # A turn_end hook/tick may have removed the last entity (e.g. a
        # lethal DoT — directly, or routed to a parent via a part tick).
        # With no one left, end here rather than advancing into an empty
        # turn order.
        if not self.turn_order:
            self.history.record_turn(self)
            return (None, log)
        self._advance_index(log)
        # _advance_index's round-wrap hooks (on_round_end/start ticks) can
        # likewise empty the order; re-check before reading the next entity.
        if not self.turn_order:
            self.history.record_turn(self)
            return (None, log)
        # Skip over any entity carrying a skip-status flag.
        eligible = self._skip_to_eligible(log)
        # _skip_to_eligible's internal round-wrap hooks can empty the order;
        # re-check before reading the next entity (mirrors the guard above).
        if not self.turn_order:
            self.history.record_turn(self)
            return (None, log)
        new_cur = self.turn_order[self.active_index]
        if eligible:
            log.extend(self.fire_hook(
                "on_turn_start", target_ids=[new_cur],
                own_only_targets=self._attached_tick_parts([new_cur])))
            log.extend(self.fire_status_tick("turn_start"))
            log.extend(self.fire_tile_time_hooks("on_turn_start"))
            log.extend(self.fire_zone_time_hooks("on_turn_start"))
            log.extend(self.fire_scheduled_turn(new_cur))
            for pid in self._attached_tick_parts([new_cur]):
                log.extend(self.fire_scheduled_turn(pid))
        else:
            log.append(
                "⏭️ every entity is skippable; the round passes "
                "without anyone acting."
            )
        self.history.record_turn(self)
        return (new_cur, log)

    # ---- Active Time Battle ------------------------------------------------
    @staticmethod
    def _atb_clean_num(x: float):
        """Int when integral, else float — keeps charge bars tidy."""
        return int(x) if float(x).is_integer() else float(x)

    def _atb_threshold_value(self) -> float:
        try:
            return float(self.rules.get("atb_threshold", 100))
        except (TypeError, ValueError):
            return 100.0

    def _atb_charge_rate(self, e: "Entity") -> float:
        """Evaluate atb_charge_formula for `e` (self=e) → per-tick rate.
        Malformed / non-numeric → 0.0 (the entity can't charge)."""
        src = str(self.rules.get("atb_charge_formula", "")).strip()
        if not src:
            return 0.0
        from formula import FormulaEngine, EvalCtx, FormulaError
        try:
            val = FormulaEngine(self).eval_expression(
                src, EvalCtx(this=e.id, target=e.id))
        except (FormulaError, Exception):
            return 0.0
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return 0.0
        return float(val)

    def _atb_reset_actor(self, e: "Entity") -> List[str]:
        """Reset the actor's charge bar after a (taken or skipped) turn.
        Empty atb_reset_formula = built-in subtract-threshold (keep overflow);
        a set formula runs with self=e. Falls back to the built-in on a
        formula error so a GM typo can't lock an entity at a full bar."""
        cvar = str(self.rules.get("atb_charge_var", "atb_charge"))
        thr = self._atb_threshold_value()
        src = str(self.rules.get("atb_reset_formula", "")).strip()

        def _builtin():
            cur = float(e.vars.get(cvar, 0) or 0)
            e.write_var(cvar, self._atb_clean_num(cur - thr))

        if not src:
            _builtin()
            return []
        from formula import FormulaEngine, EvalCtx, FormulaError
        try:
            FormulaEngine(self).eval_program(
                src, EvalCtx(this=e.id, target=e.id))
        except FormulaError as ex:
            _builtin()
            return [f"⚠️ atb_reset_formula failed for `{e.id}` ({ex}); "
                    f"subtracted threshold instead."]
        return []

    def _atb_select(self, log: List[str]) -> Optional[str]:
        """Advance every chargeable entity's bar to the soonest fill, pick
        that entity as the actor, reset its bar, and set active_index to its
        slot. Returns the actor id, or None if nobody can charge (every alive
        member has a rate <= 0). A skippable actor is NOT re-selected here —
        each next_turn is ONE entity's turn (a fast but stunned unit burns
        frequent wasted turns while a slow unit charges, which is the point of
        ATB); skip handling lives in _atb_next_turn."""
        cvar = str(self.rules.get("atb_charge_var", "atb_charge"))
        thr = self._atb_threshold_value()
        entries = []  # (time_to_fill, eid, rate)
        for eid in list(self.turn_order):
            e = self.entities.get(eid)
            if e is None or not e.is_alive:
                continue
            rate = self._atb_charge_rate(e)
            if rate <= 0:
                continue
            cur = float(e.vars.get(cvar, 0) or 0)
            entries.append((max(0.0, (thr - cur) / rate), eid, rate))
        if not entries:
            log.append("⚠️ ATB: no entity has a positive charge rate — "
                       "no one can act.")
            return None
        # Soonest fills; ties broken by higher rate, then id (stable).
        entries.sort(key=lambda t: (t[0], -t[2], t[1]))
        min_time = entries[0][0]
        if min_time > 0:
            for (_t, eid, rate) in entries:
                e = self.entities[eid]
                e.write_var(cvar, self._atb_clean_num(
                    float(e.vars.get(cvar, 0) or 0) + rate * min_time))
        actor = entries[0][1]
        try:
            self.active_index = self.turn_order.index(actor)
        except ValueError:
            self.active_index = 0
        # Taking (or skipping) a turn spends the bar.
        log.extend(self._atb_reset_actor(self.entities[actor]))
        return actor

    def _atb_turn_phase(self, eid: str, when: str, act: bool) -> List[str]:
        """Fire one turn-phase surface for the ATB actor (active_index must
        already point at it). Status ticks fire ALWAYS — even on a skipped
        turn — so DoTs/stuns can decay (there are no round ticks under ATB).
        The action surface (tile/zone time-hooks, on_turn_* passives,
        schedule_on) fires only when `act` (the entity isn't skipped)."""
        log = list(self.fire_status_tick(when))
        if act:
            hook = "on_" + when
            log.extend(self.fire_tile_time_hooks(hook))
            log.extend(self.fire_zone_time_hooks(hook))
            log.extend(self.fire_hook(
                hook, target_ids=[eid],
                own_only_targets=self._attached_tick_parts([eid])))
            if when == "turn_start":
                log.extend(self.fire_scheduled_turn(eid))
                for pid in self._attached_tick_parts([eid]):
                    log.extend(self.fire_scheduled_turn(pid))
        return log

    def _has_round_logic(self) -> bool:
        """Whether any round-coupled behavior exists (used to warn once that
        it's dormant under ATB): on_round_* passives (global/team/entity),
        round status ticks (global rule or a status def), round schedules, or
        tile/zone on_round_* time-hooks."""
        rnames = ("on_round_start", "on_round_end")
        for p in self.global_passives.values():
            if p.when in rnames:
                return True
        for ps in self.team_passives.values():
            for p in ps.values():
                if p.when in rnames:
                    return True
        for e in self.entities.values():
            for p in e.passives.values():
                if p.when in rnames:
                    return True
        if str(self.rules.get("status_tick_when", "never")) in rnames and \
                str(self.rules.get("status_tick_formula", "")).strip():
            return True
        for sdef in self.status_definitions.values():
            if str(sdef.get("tick_when", "turn_end")) in rnames and \
                    str(sdef.get("tick", "")).strip():
                return True
        if any(s.get("kind") == "round" for s in self.scheduled):
            return True
        for tpl in self.tile_templates.values():
            if any(k in (tpl.hooks or {}) for k in rnames):
                return True
        for cell in self.tiles.values():
            h = cell.get("hooks") if isinstance(cell, dict) else None
            if isinstance(h, dict) and any(k in h for k in rnames):
                return True
        for z in self.zones.values():
            h = z.get("hooks") if isinstance(z, dict) else None
            if isinstance(h, dict) and any(k in h for k in rnames):
                return True
        return False

    def _atb_next_turn(self) -> Tuple[Optional[str], List[str]]:
        """ATB turn step: fire the outgoing actor's turn_end surface, select
        the next actor by charge bar, fire its turn_start surface. NO round
        hooks/ticks/schedules ever fire (rounds are disabled under ATB)."""
        log: List[str] = []
        if not self._atb_round_warned and self._has_round_logic():
            self._atb_round_warned = True
            log.append("⚠️ ATB is active: rounds are disabled — on_round_* "
                       "hooks, round status ticks, and round schedules will "
                       "NOT fire.")
        # turn_end for the outgoing actor (active_index still points at it).
        # If its turn was skipped, only its status ticks fire (decay), not
        # its action surface (act=not _atb_last_skipped).
        if self.round_started and 0 <= self.active_index < len(self.turn_order):
            cur = self.turn_order[self.active_index]
            if cur in self.entities:
                log.extend(self._atb_turn_phase(
                    cur, "turn_end", act=not self._atb_last_skipped))
        self.round_started = True
        if not self.turn_order:
            self.history.record_turn(self)
            return (None, log)
        new_cur = self._atb_select(log)
        if new_cur is None:
            self.history.record_turn(self)
            return (None, log)
        # A skipped actor's turn still ELAPSES (bar already reset, status ticks
        # fire) but it performs no action.
        skipping = self._skipping_statuses(self.entities[new_cur])
        self._atb_last_skipped = bool(skipping)
        if skipping:
            log.append(f"⏭️ `{new_cur}`'s turn skipped "
                       f"({', '.join(sorted(skipping))}).")
        # turn_start for the new actor (active_index now points at it).
        log.extend(self._atb_turn_phase(
            new_cur, "turn_start", act=not skipping))
        self.history.record_turn(self)
        return (new_cur, log)

    def _advance_index(self, log: List[str]) -> None:
        """Advance active_index by one, handling round wrap: fire
        on_round_end + status_tick(round_end), flush any deferred
        turn-order rebuild, bump the round number, fire on_round_start
        + status_tick(round_start), and autosave the round. Factored
        out of next_turn so the skip-status loop can reuse the exact
        same wrap bookkeeping for each cell it steps over."""
        # A preceding hook/tick may have emptied the turn order (e.g. a
        # lethal DoT removed the last combatant). Nothing to advance to —
        # bail before the modulo divides by zero.
        if not self.turn_order:
            return
        new_index = (self.active_index + 1) % len(self.turn_order)
        wrapped = (new_index == 0)
        if wrapped:
            log.extend(self.fire_hook(
                "on_round_end",
                own_only_targets=self._attached_tick_parts(list(self.turn_order))))
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
            log.extend(self.fire_hook(
                "on_round_start",
                own_only_targets=self._attached_tick_parts(list(self.turn_order))))
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
        n = len(self.turn_order)   # hard cap vs. a skip-hook that GROWS the order
        checked = 0
        while checked < n:
            # A skip's round-wrap (or a skip-status side effect) can empty
            # the order; stop rather than indexing into nothing.
            if not self.turn_order:
                return False
            if self.active_index >= len(self.turn_order):
                self.active_index = 0
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
            # Bound by the CURRENT order size, not the stale `n`. If a
            # round-wrap hook SHRANK the order mid-skip, `n` over-counts and
            # the loop would keep cycling the survivors — firing extra round
            # wraps (inflating round_number) before exhausting `n`. Once we've
            # taken a full cycle's worth of steps for the live order and found
            # nobody eligible, stop.
            if checked >= len(self.turn_order):
                return False
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

    # ---- footprint-aware movement hook firing ------------------------
    # A large entity fires tile/zone movement hooks per CELL it covers:
    # on_exit / on_cell_exit for every cell the footprint vacates,
    # on_enter / on_cell_enter for every cell it newly covers, and
    # on_stop / on_cell_stop for every final cell — so a 2×2 crossing a
    # band of fire tiles burns once per fire cell entered. Boundary zone
    # hooks (on_enter/on_exit/on_stop) fire ONCE per zone (at the first
    # relevant cell); per-cell zone hooks fire per covered cell. For a
    # 1×1 entity these reduce exactly to the single-cell firings.

    def _zones_over(self, cells) -> set:
        names: set = set()
        for (cx, cy) in cells:
            names |= set(self.zones_at(cx, cy))
        return names

    def fire_footprint_tile_exit(self, eid: Optional[str],
                                 old_cells, new_cells) -> List[str]:
        nc = set(new_cells)
        log: List[str] = []
        for (cx, cy) in old_cells:
            if (cx, cy) not in nc:
                log.extend(self.fire_tile_hook("on_exit", eid, cx, cy))
        return log

    def fire_footprint_tile_enter(self, eid: Optional[str],
                                  old_cells, new_cells) -> List[str]:
        oc = set(old_cells)
        log: List[str] = []
        for (cx, cy) in new_cells:
            if (cx, cy) not in oc:
                log.extend(self.fire_tile_hook("on_enter", eid, cx, cy))
        return log

    def fire_footprint_tile_stop(self, eid: Optional[str], cells) -> List[str]:
        log: List[str] = []
        for (cx, cy) in cells:
            log.extend(self.fire_tile_hook("on_stop", eid, cx, cy))
        return log

    def fire_footprint_zone_exit(self, eid: Optional[str],
                                 old_cells, new_cells) -> List[str]:
        """PRE-move: per-cell on_cell_exit for every vacated cell in a
        zone, plus boundary on_exit ONCE for each zone the whole footprint
        is leaving (was overlapped, now disjoint)."""
        nc = set(new_cells)
        leaving = self._zones_over(old_cells) - self._zones_over(new_cells)
        log: List[str] = []
        for (cx, cy) in old_cells:
            if (cx, cy) in nc:
                continue
            for name in sorted(set(self.zones_at(cx, cy))):
                if name in leaving:
                    log.extend(self.fire_zone_hook("on_exit", name, eid, cx, cy))
                    leaving.discard(name)
                log.extend(self.fire_zone_hook("on_cell_exit", name, eid, cx, cy))
        return log

    def fire_footprint_zone_enter(self, eid: Optional[str],
                                  old_cells, new_cells,
                                  is_final: bool) -> List[str]:
        """POST-move: boundary on_enter ONCE for each newly-overlapped
        zone + per-cell on_cell_enter for every newly-covered cell in a
        zone. When is_final, also fire the footprint stop hooks."""
        oc = set(old_cells)
        entering = self._zones_over(new_cells) - self._zones_over(old_cells)
        log: List[str] = []
        for (cx, cy) in new_cells:
            if (cx, cy) in oc:
                continue
            for name in sorted(set(self.zones_at(cx, cy))):
                if name in entering:
                    log.extend(self.fire_zone_hook("on_enter", name, eid, cx, cy))
                    entering.discard(name)
                log.extend(self.fire_zone_hook("on_cell_enter", name, eid, cx, cy))
        if is_final:
            log.extend(self.fire_footprint_zone_stop(eid, new_cells))
        return log

    def fire_footprint_zone_stop(self, eid: Optional[str], cells) -> List[str]:
        """Boundary on_stop ONCE per zone the footprint ends in, plus
        per-cell on_cell_stop for every final cell in a zone."""
        log: List[str] = []
        stopped: set = set()
        for (cx, cy) in cells:
            for name in sorted(set(self.zones_at(cx, cy))):
                if name not in stopped:
                    log.extend(self.fire_zone_hook("on_stop", name, eid, cx, cy))
                    stopped.add(name)
                log.extend(self.fire_zone_hook("on_cell_stop", name, eid, cx, cy))
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

    # ---- status definitions (self-describing statuses) --------------
    # A status DEFINITION holds the behaviour a status of that name has:
    #   tick       formula body run at tick time (the DoT/regen effect;
    #              also where the GM decrements `duration` and removes the
    #              status, since auto-decay is deliberately not built in)
    #   tick_when  when the tick fires (turn_end default / turn_start /
    #              round_start / round_end / never)
    #   stack      re-application mode (see apply_status / the
    #              status_default_stack rule); "" = use the rule default
    #   max_level  cap for the add_level stack mode (0 = uncapped)
    #   data       default data merged into a freshly-applied instance
    # The instance on an entity (entity.status[name]) carries only the
    # dynamic state (level / duration / whatever the def's data seeds).

    def define_status(self, name: str, *, overwrite: bool = False) -> Dict[str, Any]:
        """Create an empty status definition. Raises DuplicateId if it
        already exists unless overwrite=True."""
        if not isinstance(name, str) or not name.strip():
            raise VTTError("status definition name must be a non-empty string.")
        if not overwrite and name in self.status_definitions:
            raise DuplicateId(f"Status definition '{name}' already exists.")
        self.status_definitions[name] = {
            "tick": "", "tick_when": "turn_end",
            "stack": "", "max_level": 0, "data": {},
            "tags": [], "removes": "", "blocked_by": "",
            # Graphics overlay (drawn over an afflicted entity). Empty = none.
            "sprite": "", "sprite_opacity": None,
            "sprite_tint": "", "sprite_layer": None,
        }
        return self.status_definitions[name]

    def remove_status_def(self, name: str) -> None:
        """Delete a status definition. Existing instances of that name on
        entities are left in place (they just lose their tick behaviour
        and fall back to the global formula, like an orphaned tile)."""
        if name not in self.status_definitions:
            raise NotFound(f"Status definition '{name}' not found.")
        del self.status_definitions[name]

    # ---- status tags / cross-status / resistance helpers --------------
    # A "token" is either a bare status NAME (matches that status) or
    # `tag:<x>` (matches any status whose DEFINITION carries tag <x>).
    # Shared by removes / blocked_by (cross-status) and immune / resist.

    def status_def_tags(self, name: str) -> List[str]:
        """The tag list declared on a status DEFINITION (empty if the def
        is absent or untagged). Tags are a definition-level category."""
        d = self.status_definitions.get(name)
        tags = d.get("tags") if isinstance(d, dict) else None
        if isinstance(tags, str):
            return [t.strip() for t in tags.split(",") if t.strip()]
        if isinstance(tags, (list, tuple)):
            return [str(t) for t in tags]
        return []

    def _status_token_matches(self, token: str, status_name: str) -> bool:
        """Whether `token` (a name or `tag:<x>`) matches the status named
        `status_name` (a tag token checks that status's definition tags)."""
        token = token.strip()
        if not token:
            return False
        if token.startswith("tag:"):
            return token[4:].strip() in self.status_def_tags(status_name)
        return token == status_name

    @staticmethod
    def _token_list(raw: Any) -> List[str]:
        """Normalize a removes/blocked_by/immune field (list or CSV)."""
        if isinstance(raw, str):
            return [t.strip() for t in raw.split(",") if t.strip()]
        if isinstance(raw, (list, tuple)):
            return [str(t).strip() for t in raw if str(t).strip()]
        return []

    def _statuses_matching_tokens(self, e: "Entity",
                                  tokens: List[str]) -> List[str]:
        """Names of statuses currently on `e` that match ANY of `tokens`."""
        out = []
        for sname in e.status:
            if any(self._status_token_matches(tok, sname) for tok in tokens):
                out.append(sname)
        return out

    def _effective_status_resist_sources(self, e: "Entity") -> List[str]:
        """The vars roots scanned for status_resist / status_immune records:
        the per-entity `__status_resist_sources` var (replaces the default)
        or the status_resist_sources rule, plus `__status_resist_sources_add`
        (extends). Mirrors _effective_modifier_sources."""
        override = e.vars.get("__status_resist_sources")
        if isinstance(override, list):
            roots = [str(r) for r in override]
        else:
            roots = [r.strip() for r in
                     str(self.rules.get("status_resist_sources", "equipped")).split(",")
                     if r.strip()]
        add = e.vars.get("__status_resist_sources_add")
        if isinstance(add, list):
            roots = roots + [str(r) for r in add]
        return roots

    def _gather_resist_records(self, e: "Entity") -> Tuple[List[Any], List[dict]]:
        """Collect (immune-token-lists, resist-maps) contributing to `e`:
        the direct innate `status_immune`/`status_resist` vars, plus every
        nested `status_immune`/`status_resist` found anywhere under a scanned
        source root (so an equipped item grants, an inventoried one doesn't)."""
        immune: List[Any] = []
        resist: List[dict] = []
        di = e.vars.get("status_immune")
        if di is not None:
            immune.append(di)
        dr = e.vars.get("status_resist")
        if isinstance(dr, dict):
            resist.append(dr)

        def _walk(node: Any) -> None:
            if not isinstance(node, dict):
                return
            for key, val in node.items():
                if key == "status_immune" and val is not None:
                    immune.append(val)
                elif key == "status_resist" and isinstance(val, dict):
                    resist.append(val)
                elif isinstance(val, dict):
                    _walk(val)

        for root in self._effective_status_resist_sources(e):
            node: Any = e.vars
            ok = True
            for seg in root.split("."):
                if isinstance(node, dict) and seg in node:
                    node = node[seg]
                else:
                    ok = False
                    break
            if ok:
                _walk(node)
        return immune, resist

    def status_resistance(self, eid: str, name: str) -> Tuple[bool, int]:
        """(is_immune, level_reduction) for status `name` applied to `eid`,
        aggregated from the entity's innate + source-gated resistance
        records per the status_resist_stack rule. Immunity if any matching
        immune token; reduction combines matching resist values."""
        e = self.entities.get(eid)
        if e is None:
            return (False, 0)
        immune_lists, resist_maps = self._gather_resist_records(e)
        for lst in immune_lists:
            for tok in self._token_list(lst):
                if self._status_token_matches(tok, name):
                    return (True, 0)
        stack = str(self.rules.get("status_resist_stack", "sum"))
        amounts: List[int] = []
        for rmap in resist_maps:
            for key, val in rmap.items():
                if not self._status_token_matches(str(key), name):
                    continue
                try:
                    amounts.append(int(val))
                except (TypeError, ValueError):
                    continue
        if not amounts:
            return (False, 0)
        if stack == "max":
            reduction = max(amounts)
        elif stack == "first":
            reduction = amounts[0]
        else:  # sum
            reduction = sum(amounts)
        return (False, max(0, reduction))

    @staticmethod
    def _cap_status_level(sdef: Dict[str, Any], lvl: int) -> int:
        """Clamp a status level to the definition's max_level (a hard ceiling
        on the level field — applied to first application, replace, and
        add_level alike). max_level absent / <=0 = uncapped."""
        try:
            maxl = int(sdef.get("max_level", 0) or 0)
        except (TypeError, ValueError):
            maxl = 0
        return min(lvl, maxl) if maxl > 0 else lvl

    def _resistance_applies(self, e: "Entity", name: str,
                            sdef: Dict[str, Any], level_given: bool) -> bool:
        """Whether a flat level-reduction resistance applies to THIS status
        application — only when a level is actually added/set: a first
        application, an add_level increment (implicit +1 OR explicit), or a
        replace with an explicit level. refresh / extend / none set no level,
        so resistance has nothing to reduce (and must not block a
        duration-only refresh)."""
        if name not in e.status:
            return True  # first application seeds a level
        mode = sdef.get("stack") or str(
            self.rules.get("status_default_stack", "refresh"))
        if mode == "add_level":
            return True
        if mode == "replace":
            return level_given
        return False  # refresh / extend / none — no level applied

    def status_apply_block_reason(self, eid: str, name: str,
                                  level: Optional[int] = None) -> Optional[str]:
        """A human reason the status would NOT apply to `eid` (immunity,
        a blocking status, or full resistance), or None if it would. Used
        for command feedback; recomputes the same checks apply_status runs."""
        e = self.entities.get(eid)
        if e is None:
            return None
        immune, reduction = self.status_resistance(eid, name)
        if immune:
            return f"immune to `{name}`"
        sdef = self.status_definitions.get(name) or {}
        blockers = self._statuses_matching_tokens(
            e, self._token_list(sdef.get("blocked_by")))
        if blockers:
            return f"`{name}` blocked by `{blockers[0]}`"
        if reduction and self._resistance_applies(e, name, sdef, level is not None):
            base = int(level) if level is not None else 1
            if base - reduction <= 0:
                return f"`{name}` fully resisted (reduction {reduction})"
        return None

    def apply_status(self, eid: str, name: str,
                     level: Optional[int] = None,
                     duration: Optional[int] = None,
                     *, force: bool = False) -> List[str]:
        """Apply status `name` to entity `eid`, honoring the definition's
        `stack` mode (else the status_default_stack rule) when the status
        is already present. A FIRST application seeds the definition's
        default `data`, then sets level (default 1) and duration. Fires
        the on_status_added / on_status_changed hooks via the status
        chokepoint. Returns the hook log. Raises NotFound for an unknown
        entity.

        `force=True` (the force_status / `!status force` path) skips the
        immunity + resistance gating entirely — the level/increment is
        applied regardless of resistance. blocked_by (cross-status) and the
        part immune/redirect rules are still honored; force is specifically
        the 'ignore resistance' axis."""
        e = self.entities.get(eid)
        if e is None:
            raise NotFound(f"Entity '{eid}' not found.")
        # Body-part status rules: a part can be immune to some statuses
        # (no-op) or redirect them to its parent (per-entity DoT). Raw
        # `!ent status` editing bypasses this — only the apply path honors it.
        if e.is_part:
            if name in self._part_status_names(
                    e, "__status_immune", "part_status_immune"):
                return []
            if name in self._part_status_names(
                    e, "__status_redirect", "part_status_redirect"):
                if e.part_of in self.entities:
                    return self.apply_status(e.part_of, name, level, duration,
                                             force=force)
                return []
        sdef = self.status_definitions.get(name) or {}
        new_level = None if level is None else int(level)
        new_duration = None if duration is None else int(duration)
        # Cross-status blocking is ALWAYS honored (a different status the
        # target already has prevents this one — independent of resistance).
        blockers = self._statuses_matching_tokens(
            e, self._token_list(sdef.get("blocked_by")))
        if blockers:
            return []
        # Resistance / immunity gating — skipped entirely when force=True.
        # Resistance reduces a LEVEL being added/set, mode-aware (via
        # _resistance_applies) so an implicit add_level +1 is resisted just
        # like an explicit level while a duration-only refresh/extend is
        # never blocked; if the reduced level drops to <=0 the application is
        # fully resisted (no-op).
        if not force:
            immune, reduction = self.status_resistance(eid, name)
            if immune:
                return []
            if reduction and self._resistance_applies(
                    e, name, sdef, new_level is not None):
                base = new_level if new_level is not None else 1
                eff = base - reduction
                if eff <= 0:
                    return []
                new_level = eff
        before = copy.deepcopy(e.status.get(name))
        if name not in e.status:
            inst = copy.deepcopy(sdef.get("data")) if isinstance(sdef.get("data"), dict) else {}
            seed_lv = new_level if new_level is not None else int(inst.get("level", 1))
            inst["level"] = self._cap_status_level(sdef, seed_lv)
            if new_duration is not None:
                inst["duration"] = new_duration
            e.status[name] = inst
        else:
            mode = sdef.get("stack") or str(self.rules.get("status_default_stack", "refresh"))
            inst = e.status[name]
            if mode == "none":
                pass
            elif mode == "replace":
                if new_level is not None:
                    inst["level"] = self._cap_status_level(sdef, new_level)
                if new_duration is not None:
                    inst["duration"] = new_duration
            elif mode == "extend":
                if new_duration is not None:
                    inst["duration"] = int(inst.get("duration", 0)) + new_duration
            elif mode == "add_level":
                add = new_level if new_level is not None else 1
                nl = int(inst.get("level", 0)) + add
                inst["level"] = self._cap_status_level(sdef, nl)
                if new_duration is not None:
                    inst["duration"] = new_duration
            else:  # refresh (default)
                if new_duration is not None:
                    inst["duration"] = new_duration
        after = copy.deepcopy(e.status.get(name))
        log = ([] if before == after
               else self._emit_status_diff(eid, name, before, after))
        # removes: an accepted application clears the statuses this one
        # cancels (burn removes freeze). Fires even on a no-change refresh
        # so re-applying still purges the opposite. Tokens are names or
        # `tag:<x>`; the status itself is never self-removed.
        for other in self._statuses_matching_tokens(
                e, self._token_list(sdef.get("removes"))):
            if other == name or other not in e.status:
                continue
            ob = copy.deepcopy(e.status[other])
            del e.status[other]
            log = log + self._emit_status_diff(eid, other, ob, None)
        return log

    def dispel_statuses(self, eid: str, token: Any,
                        max_count: int = 0) -> Tuple[int, List[str]]:
        """Remove every status on `eid` matching `token` — a status name, a
        `tag:<x>` token, or a CSV of either (the same token convention as
        removes / blocked_by). `max_count` > 0 caps how many are removed
        (sorted by name for determinism); 0 = all. Each removal goes through
        the status diff chokepoint, so on_status_removed fires. Returns
        (count_removed, log). Token-only — there is no 'undispellable' guard;
        keep un-strippable effects outside the token's range. Raises NotFound
        for an unknown entity."""
        e = self.entities.get(eid)
        if e is None:
            raise NotFound(f"Entity '{eid}' not found.")
        matches = sorted(self._statuses_matching_tokens(
            e, self._token_list(token)))
        if isinstance(max_count, int) and not isinstance(max_count, bool) \
                and max_count > 0:
            matches = matches[:max_count]
        log: List[str] = []
        removed = 0
        for sname in matches:
            if sname not in e.status:
                continue
            before = copy.deepcopy(e.status[sname])
            del e.status[sname]
            log += self._emit_status_diff(eid, sname, before, None)
            removed += 1
        return removed, log

    def transfer_status(self, from_eid: str, to_eid: str,
                        name: str) -> Tuple[bool, List[str]]:
        """Move status `name` from `from_eid` to `to_eid`. It LEAVES the
        source unconditionally (on_status_removed fires) and RE-APPLIES on the
        destination via apply_status — so the destination's stacking mode +
        resistance/immunity/blocked_by all apply (a RESISTIBLE move: if the
        dest resists or is immune, the status is consumed — gone from the
        source, doesn't stick). Carries level + duration; custom instance data
        re-seeds from the definition, exactly like the part_status_redirect
        path. Returns (landed_on_dest, log). No-op (False) if the source lacks
        the status or from==to. Raises NotFound for an unknown entity."""
        src = self.entities.get(from_eid)
        if src is None:
            raise NotFound(f"Entity '{from_eid}' not found.")
        if to_eid not in self.entities:
            raise NotFound(f"Entity '{to_eid}' not found.")
        if not isinstance(name, str) or not name:
            raise VTTError("transfer_status: name must be a non-empty string.")
        if from_eid == to_eid or name not in src.status:
            return False, []
        inst = copy.deepcopy(src.status[name])
        lv = inst.get("level")
        du = inst.get("duration")
        lv = int(lv) if isinstance(lv, (int, float)) and not isinstance(lv, bool) else None
        du = int(du) if isinstance(du, (int, float)) and not isinstance(du, bool) else None
        # Leave the source first (it's moving regardless of whether it sticks).
        del src.status[name]
        log = self._emit_status_diff(from_eid, name, inst, None)
        # The source's on_status_removed hook could have removed the dest.
        if to_eid not in self.entities:
            return False, log
        log += self.apply_status(to_eid, name, lv, du)
        landed = (to_eid in self.entities
                  and name in self.entities[to_eid].status)
        return landed, log

    def fire_status_tick(self, when: str) -> List[str]:
        """Run each status's tick at the given `when`. A status WITH a
        matching definition runs that definition's `tick` formula when the
        definition's `tick_when` equals `when`; a status with NO definition
        falls back to the global status_tick_formula rule at the global
        status_tick_when (backwards compatible). Returns accumulated log
        lines (one ⚠️ per formula failure).

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
        # Targeting depends only on `when`: a turn tick hits the entity
        # whose turn it is; a round tick hits everyone in turn order.
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

        # Attached parts share their parent's status clock (a glued/region/
        # located part has no initiative, so its statuses would otherwise
        # never tick). Each part's own definition tick_when still gates
        # whether it fires for this `when`.
        targets = targets + self._attached_tick_parts(targets)

        global_when = self.rules.get("status_tick_when", "never")
        global_src = self.rules.get("status_tick_formula", "")
        global_active = (global_when == when and isinstance(global_src, str)
                         and bool(global_src.strip()))

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
                # A PRIOR status's tick may have killed/removed this entity
                # (e.g. a lethal DoT). Its remaining statuses must not tick
                # from beyond the grave — same invariant the ghost-passive
                # guards enforce for the hook/event/var-hook firing sites.
                if eid not in self.entities:
                    break
                if sname not in e.status:
                    continue
                # A status WITH a definition runs its own tick at its own
                # tick_when (default turn_end); a def-less status falls back
                # to the global status_tick_formula at the global when.
                sdef = self.status_definitions.get(sname)
                if sdef is not None:
                    body = sdef.get("tick")
                    if not (isinstance(body, str) and body.strip()):
                        continue
                    def_when = sdef.get("tick_when") or "turn_end"
                    if def_when != when:
                        continue
                    src = body
                else:
                    if not global_active:
                        continue
                    src = global_src
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
        for p in self._firing_passives(entity_id):
            if p.when == when:
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=entity_id, is_global=True,
                ))
        # A global/team handler may have removed the entity (kill/remove);
        # don't fire its own passives from beyond the grave.
        if entity_id in self.entities:
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
        # Re-stamp any aura anchored to this entity so it rides along with
        # the move (before on_entity_moved passives fire, so they observe
        # the repositioned aura). Like zone_shift, the restamp just moves
        # cells — it does NOT fire the aura's own enter/exit hooks for
        # entities it sweeps over.
        self._restamp_anchors_for(entity_id)
        # Glue attached body parts to the parent's new cell (before
        # on_entity_moved passives fire, so they observe the moved parts).
        self._restamp_parts_for(entity_id)
        # Carry mounted riders along with the vehicle (each to its slot's
        # cell — region cell for a visible slot, the anchor otherwise). Like
        # the part/anchor restamp, this just moves cells; it does NOT fire
        # the riders' own movement hooks (they're carried, not walking).
        self._restamp_riders_for(entity_id)
        # Snake body: a stepwise move already trailed the chain per
        # fire_entity_step (which left __seg_last == the head's cell). A
        # discontinuous move (teleport / swap / push with no per-cell steps)
        # leaves __seg_last stale, so re-lay the body straight behind the head.
        if self.is_snake_head(entity_id):
            last = e.vars.get("__seg_last")
            if not (isinstance(last, list) and len(last) == 2
                    and last[0] == e.x and last[1] == e.y):
                self._resettle_snake(entity_id)
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
        for p in self._firing_passives(entity_id):
            if p.when == "on_entity_moved":
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=entity_id, is_global=True,
                ))
        for p in e.passives.values():
            if p.when == "on_entity_moved":
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=entity_id, is_global=False,
                ))
        # Fog memory: a move can reveal new cells for the mover's team —
        # fold current vision into its explored set (no-op unless memory
        # on). Other entities a passive moved record via their own fires.
        self._record_vision(getattr(e, "team", None))
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
        # Snake body: trail the chain one cell into the head's vacated
        # position (from_x, from_y), before passives observe the move.
        if self.is_snake_head(entity_id):
            self._advance_snake(entity_id, from_x, from_y)
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
        for p in self._firing_passives(entity_id):
            if p.when == "on_entity_step":
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=entity_id, is_global=True,
                ))
        for p in e.passives.values():
            if p.when == "on_entity_step":
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=entity_id, is_global=False,
                ))
        # Fog memory: capture cells revealed mid-path, per step (the
        # once-per-move record in fire_entity_moved would miss a stepwise
        # move's intermediate vision).
        self._record_vision(getattr(e, "team", None))
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
        for p in self._firing_passives(actor_id):
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
        for p in self._firing_passives(target_id):
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
        for p in self._firing_passives(actor_id):
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

        # Build the concrete entity FIRST (so its footprint — including any
        # size supplied by default_entity_vars — is known before we search
        # for / validate a placement). Deep-copy so the summoned entity
        # doesn't alias the stored template's nested dicts (mutating the
        # minion later must not edit the stored blueprint).
        prefix = id_prefix or template.get("name") or template.get("id") or "summon"
        new_id = self.mint_entity_id(prefix)
        d = copy.deepcopy(template)
        d["id"] = new_id
        e = Entity.from_dict(d)
        # Apply default vars to the probe so the footprint size reflects
        # them; spawn re-applies (fill-only, idempotent).
        self._apply_default_vars(e)
        stackable_template = e.is_cell_stackable

        place_x, place_y = x, y
        if near_radius is not None:
            place = self._find_free_cell_near(x, y, int(near_radius), e=e)
            if place is None:
                fw, fh = self.entity_footprint(e)
                raise Occupied(
                    f"summon_near: no cell within radius {near_radius} of "
                    f"({x},{y}) fits `{new_id}`'s {fw}x{fh} footprint."
                )
            place_x, place_y = place
        else:
            # Footprint-aware pre-check for a friendly message; spawn's
            # _validate_placement is the authoritative gate.
            if not self.footprint_in_bounds(e, place_x, place_y):
                raise OutOfBounds(
                    f"summon: footprint anchored at ({place_x},{place_y}) is "
                    f"off-grid ({self.grid_width}x{self.grid_height})."
                )
            if not stackable_template and any(
                    self.cell_occupant(cx, cy) is not None
                    for cx, cy in self.entity_cells(e, place_x, place_y)):
                raise Occupied(
                    f"summon: footprint at ({place_x},{place_y}) is occupied. "
                    f"Use summon_near to search for a free cell."
                )

        d["x"] = place_x
        d["y"] = place_y
        e.x, e.y = place_x, place_y
        # Entity.spawn validates bounds/occupancy again (cheap) and
        # fires on_entity_spawned. We pre-validated for a clean error
        # message; spawn's checks are the authoritative gate.
        self._summon_count += 1
        _, log = e.spawn(self, place_x, place_y)
        # Consume a `parts` body spec (locational damage): a dict
        # {role: part-template} or a list of part-template dicts. Each is
        # auto-spawned at the parent's cell and linked via part_of. A dict
        # key supplies the part's role name (part_name var) when the
        # template doesn't carry one. Reserved top-level template key, so
        # Entity.from_dict ignores it for the parent itself.
        parts_spec = template.get("parts")
        if isinstance(parts_spec, dict):
            part_items = list(parts_spec.items())
        elif isinstance(parts_spec, (list, tuple)):
            part_items = [(None, pt) for pt in parts_spec]
        else:
            part_items = []
        name_var = self.part_name_var()
        for role, pt in part_items:
            if not isinstance(pt, dict):
                continue
            ptc = copy.deepcopy(pt)
            ptc.pop("x", None)
            ptc.pop("y", None)
            pvars = ptc.setdefault("vars", {})
            if role is not None and name_var not in pvars:
                pvars[name_var] = role
            pprefix = (ptc.get("id") or pvars.get(name_var)
                       or ptc.get("name") or f"{new_id}_part")
            pid = self.mint_entity_id(str(pprefix))
            ptc["id"] = pid
            ptc["x"] = place_x
            ptc["y"] = place_y
            ptc["part_of"] = new_id
            if "name" not in ptc:
                ptc["name"] = str(pvars.get(name_var, pid))
            try:
                pe = Entity.from_dict(ptc)
                _, plog = pe.spawn(self, place_x, place_y)
                log += plog
            except VTTError:
                # A malformed part entry (missing hp var, id clash) is
                # skipped rather than aborting the whole summon.
                pass
        # Snake body: a `segments` list/dict spawns SEGMENTS chained behind
        # the head (this entity), in order. Each entry: {name?, id?, hp,
        # maxhp, vars?}. add_segment finds a free trailing cell and sets the
        # __segment / __follows linkage. Reserved template key like `parts`.
        seg_spec = template.get("segments")
        if isinstance(seg_spec, dict):
            seg_items = list(seg_spec.items())
        elif isinstance(seg_spec, (list, tuple)):
            seg_items = [(None, st) for st in seg_spec]
        else:
            seg_items = []
        for role, st in seg_items:
            if not isinstance(st, dict):
                continue
            svars = copy.deepcopy(st.get("vars", {}) or {})
            if role is not None and name_var not in svars:
                svars[name_var] = role
            sname = str(st.get("name") or svars.get(name_var) or f"{new_id}_seg")
            sid = self.mint_entity_id(str(st.get("id") or sname))
            try:
                shp = int(st.get("hp", 0))
                smhp = int(st.get("maxhp", shp))
            except (TypeError, ValueError):
                continue
            try:
                _, slog = self.add_segment(new_id, sid, sname, shp, smhp, svars)
                log += slog
            except VTTError:
                pass
        return new_id, log

    def _find_free_cell_near(
        self, x: int, y: int, radius: int,
        e: Optional["Entity"] = None,
    ) -> Optional[Tuple[int, int]]:
        """Ring-search outward from (x,y) for the first in-bounds cell
        where an entity can be placed, distance 0..radius (Chebyshev).
        Returns the ANCHOR (cx,cy) or None. When `e` is given the search
        is footprint-aware: the whole W×H footprint anchored at the
        candidate must fit in bounds and (unless `e` is stackable) be
        clear; otherwise it's the plain single-cell test. Deterministic
        order: rings nearest-first, within a ring by (dy, dx) so
        placement is reproducible."""
        def fits(cx: int, cy: int) -> bool:
            if e is None:
                return self.in_bounds(cx, cy) and not self.is_occupied(cx, cy)
            if not self.footprint_in_bounds(e, cx, cy):
                return False
            if e.is_cell_stackable:
                return True
            return all(self.cell_occupant(fx, fy) is None
                       for fx, fy in self.entity_cells(e, cx, cy))
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
                if fits(cx, cy):
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
        # Body parts never die via the automatic chokepoint — their end
        # is owned by damage_part (broken-limb / vital) and the
        # parent-death cascade, so a raw hp write (or a 0/0 zone sitting
        # at hp 0) doesn't trip a spurious death.
        if e.is_part:
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
        # A body part never leaves a corpse. check_death already skips parts,
        # but the UNCONDITIONAL kill path (kill() / !ent kill, which bypasses
        # the death condition) reaches here directly — route it to limb
        # destruction instead of storing a revivable part-corpse. Only the
        # parent-death cascade puts a part's state anywhere, and that goes
        # into the PARENT's corpse, not the part's own.
        if e.is_part:
            return self._process_part_death(e)
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
                self._store_corpse(e)   # also snapshots the parts
            # Remove from match (entity.remove handles turn-order
            # bookkeeping + group scrubbing) regardless of result.
            e.remove()
            # Cascade: the creature is gone, so its body parts go with it
            # (no orphaned, suddenly-visible limbs). The WHOLE subtree —
            # parts, their parts, ... — is removed, not just direct parts,
            # or a deeper limb (wing -> feather) would survive as a free-
            # floating zombie. Their snapshots already rode into the corpse
            # above for revive. Silent — destroy effects are for targeted
            # limb destruction, not whole-creature death.
            for part in self.entity_part_subtree(entity_id):
                if part.id in self.entities:
                    part.remove()
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
        was_part = eid in self.entities and self.entities[eid].is_part
        log: List[str] = []
        if eid in self.entities:
            log = self._process_death(eid)
        # A killed body part is destroyed IN PLACE (lingers as a dead limb),
        # so "still in the match" does not mean "not killed" — count it as
        # killed once it carries the __part_destroyed latch.
        post = self.entities.get(eid)
        killed = post is None or (was_part and bool(post.vars.get("__part_destroyed")))
        return killed, log

    # ---------- locational damage: routing + part death ----------
    def is_indestructible(self, e: "Entity") -> bool:
        """True when `e` cannot die on its own: its `indestructible` var
        (default from part_indestructible_default), OR it's a body part
        with max_hp <= 0 (a 0/0 passthrough zone — always indestructible).
        Such entities are skipped by the automatic death check; only
        damage_part / kill_entity / the parent-death cascade end them."""
        if bool(e.vars.get("indestructible",
                           self.rules.get("part_indestructible_default", False))):
            return True
        if e.is_part:
            _, mhp_var, _ = e._vital_var_names()
            try:
                if int(e.vars.get(mhp_var, 0) or 0) <= 0:
                    return True
            except (TypeError, ValueError):
                return True
        return False

    def _part_transfer_base(self, amount: int, cur_hp: int, max_hp: int,
                            cap: str) -> int:
        """Damage that feeds the to-main % under cap mode `cap`. See the
        part_to_main_cap_default rule. A 0/0 part is forced to `none`."""
        if max_hp <= 0:
            return amount          # 0/0 passthrough — full hit taps through
        if cap == "max_hp":
            return min(amount, max_hp)
        if cap == "remaining_hp":
            return min(amount, max(0, cur_hp))
        if cap.startswith("absolute:"):
            try:
                return min(amount, int(cap.split(":", 1)[1]))
            except (TypeError, ValueError):
                return amount
        return amount              # "none" / unknown -> uncapped

    def damage_part(self, part_id: str, amount: int) -> Tuple[int, List[str]]:
        """Deal `amount` damage to body part `part_id`, routing the
        configured share to the parent's main HP (the HD2 'damage to main'
        model). Returns (to_main_dealt, log_lines).

        Order (so the transfer never depends on clamp timing — see CLAUDE.md):
          1. read the part's pre-hit hp + knobs;
          2. compute to_main = to_main_percent% of the cap-moded damage and
             apply it to the PARENT via the normal hp path (parent
             clamp/hooks/death all fire — this is what lets a big uncapped
             hit kill the parent);
          3. write the part's own hp, floored at 0 (a 0/0 zone stays 0);
          4. if the part hit 0 and isn't indestructible, run its death
             (fire on_death once; if `vital`, kill the parent).

        Routing happens ONLY here — a raw `entity[part].hp -= n` does not
        spill to main (intentional, mirrors damage_entity vs write_var)."""
        p = self.entities.get(part_id)
        if p is None:
            raise NotFound(f"Entity '{part_id}' not found.")
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            raise VTTError("damage_part: amount must be an integer.")
        hp_var, mhp_var, _ = p._vital_var_names()
        cur_hp = int(p.vars.get(hp_var, 0) or 0)
        max_hp = int(p.vars.get(mhp_var, 0) or 0)
        pct = float(p.vars.get("to_main_percent",
                              self.rules.get("part_to_main_percent_default", 0)) or 0)
        cap = str(p.vars.get("to_main_cap",
                            self.rules.get("part_to_main_cap_default", "max_hp")))
        log: List[str] = []
        # (2) transfer to parent main HP first, from pre-hit values.
        to_main = 0
        parent = self.entities.get(p.part_of) if p.part_of else None
        if parent is not None and pct != 0:
            base = self._part_transfer_base(amount, cur_hp, max_hp, cap)
            to_main = int(round(base * pct / 100.0))
            if to_main != 0:
                p_hp_var, _, _ = parent._vital_var_names()
                parent.write_var(p_hp_var,
                                 int(parent.vars.get(p_hp_var, 0) or 0) - to_main)
        # (3) write the part's own hp, floored at 0.
        new_hp = max(0, cur_hp - amount)
        if new_hp != cur_hp:
            p.write_var(hp_var, new_hp)
        # A heal that lifts a previously-destroyed part back above 0 clears
        # the destroyed latch so it can break (and re-fire on_death) again,
        # and RESUMES any aura suspended when it was destroyed (no-op unless
        # anchored_zone_on_anchor_loss is 'suspend').
        if new_hp > 0 and p.vars.get("__part_destroyed"):
            p.vars.pop("__part_destroyed", None)
            self._resume_anchored_zones(p.id)
        # (4) destruction.
        if new_hp <= 0 and (cur_hp - amount) <= 0 and not self.is_indestructible(p):
            log += self._process_part_death(p)
        return to_main, log

    def _process_part_death(self, p: "Entity", *, cascade: bool = False) -> List[str]:
        """A body part reaching 0 hp by damage. Fires the part's on_death
        ONCE (destroy effects live there as passives), latched by the
        `__part_destroyed` var so further hits don't re-fire. The part
        STAYS attached as a dead (0-hp) limb — the doc's broken-limb model;
        a part-targeted revive (or a heal) restores it. If the part is
        `vital`, the parent is run through the kill function. `cascade`
        (parent already dying) skips the vital re-kill."""
        if p.vars.get("__part_destroyed"):
            return []
        p.vars["__part_destroyed"] = True   # raw set: no write_var recursion
        self.log_event("part_destroyed", entity=p.id, name=p.name,
                       part_of=p.part_of or "")
        log = self.fire_hook("on_death", target_ids=[p.id])
        # A destroyed limb LINGERS attached (it doesn't route through
        # Entity.remove), so resolve its anchored auras here too — same
        # delete/freeze/suspend rule as any anchor loss. Idempotent if a
        # later cascade removes the part.
        self._release_anchored_zones(p.id)
        if not cascade:
            vital = bool(p.vars.get("vital",
                                    self.rules.get("part_vital_default", False)))
            if vital and p.part_of in self.entities:
                _, klog = self.kill_entity(p.part_of)
                log += klog
            # Snake segment: apply the sever behavior (cascade / split). Done
            # after the vital check — if `vital` already killed the head, the
            # whole-creature death cascade removed the parts and is_segment is
            # now false, so this is a no-op.
            if p.is_segment:
                log += self._sever_segment(p)
        return log

    def _sever_segment(self, p: "Entity") -> List[str]:
        """Apply a destroyed segment's `segment_death_mode`:
          cascade — destroy `p` and every segment BEHIND it (the back of the
                    worm is severed and removed; no corpses).
          split   — remove `p` (the cut), promote the segment behind it to a
                    new independent head, re-parent the rest of the tail to
                    it, and stamp segment_split_head_template.
        `none` / `solid` do nothing extra (the segment just lingers as a dead
        limb). Resolution: the segment's `__segment_death_mode` var > the
        head's > the rule."""
        head_id = p.part_of
        head = self.entities.get(head_id)
        if head is None:
            return []
        mode = str(p.vars.get("__segment_death_mode")
                   or head.vars.get("__segment_death_mode")
                   or self.rules.get("segment_death_mode", "none"))
        if mode in ("none", "solid"):
            return []
        chain = self.snake_segments(head_id)
        if p not in chain:
            return []
        k = chain.index(p)
        behind = chain[k + 1:]
        log: List[str] = []
        if mode == "cascade":
            for s in [p] + behind:
                self.log_event("segment_severed", entity=s.id,
                               mode="cascade", part_of=head_id)
                s.remove()
            log.append(
                f"`{head_id}` severed at `{p.id}`: "
                f"{len(behind) + 1} segment(s) destroyed.")
        elif mode == "split":
            if behind:
                newhead = behind[0]
                self._promote_segment_to_head(newhead, head)
                for s in behind[1:]:
                    s.part_of = newhead.id
                log.append(
                    f"`{head_id}` split at `{p.id}`: `{newhead.id}` is now an "
                    f"independent head trailing {len(behind) - 1} segment(s).")
            self.log_event("segment_severed", entity=p.id,
                           mode="split", part_of=head_id)
            p.remove()
            self._rebuild_turn_order()
        return log

    def _promote_segment_to_head(self, seg: "Entity", old_head: "Entity") -> None:
        """Turn a body segment into a free, living, independent head: clear
        its segment/part linkage, stamp the split-head template (missing keys
        only — actions/passives/AI vars), and give it the old head's
        initiative if it has none so it acts on its own."""
        for k in ("__segment", "__follows", "__part_located",
                  "__part_destroyed", "__seg_path", "__seg_last"):
            seg.vars.pop(k, None)
        seg.part_of = None
        tmpl = old_head.vars.get("__segment_split_head_template")
        if not isinstance(tmpl, dict):
            tmpl = self.rules.get("segment_split_head_template")
        self._fill_missing_vars(seg, tmpl)
        if seg.initiative is None and old_head.initiative is not None:
            seg.initiative = old_head.initiative
        seg.vars["__seg_last"] = [seg.x, seg.y]

    # ---------- status-on-part rules + AoE damage spread ----------
    def _part_status_names(self, e: "Entity", var_key: str,
                           rule_key: str) -> "set[str]":
        """The set of status names for a part-status rule: the per-part var
        (replaces the default when set) else the gamerule. Accepts a list
        or a comma-separated string."""
        raw = e.vars.get(var_key)
        if raw is None:
            raw = self.rules.get(rule_key, "")
        if isinstance(raw, str):
            return {t.strip() for t in raw.split(",") if t.strip()}
        if isinstance(raw, (list, tuple)):
            return {str(t) for t in raw}
        return set()

    def _part_aoe_weight(self, p: "Entity") -> float:
        """A part's area-attack share weight: its `aoe_weight` var, else the
        sum of its directional `hit_weights`, else part_aoe_weight_default."""
        w = p.vars.get("aoe_weight")
        if w is not None:
            try:
                return max(0.0, float(w))
            except (TypeError, ValueError):
                return 0.0
        hw = p.vars.get("hit_weights")
        if isinstance(hw, dict):
            total = 0.0
            for v in hw.values():
                try:
                    total += float(v)
                except (TypeError, ValueError):
                    pass
            if total > 0:
                return total
        try:
            return max(0.0, float(self.rules.get("part_aoe_weight_default", 1)))
        except (TypeError, ValueError):
            return 1.0

    def damage_spread(self, target_id: str, total: int,
                      mode: Optional[str] = None,
                      fragments: Optional[int] = None,
                      origin: Optional[Tuple[int, int]] = None,
                      radius: Optional[int] = None) -> Tuple[int, List[str]]:
        """Distribute `total` area damage across `target_id`'s body parts,
        returning (to_main_dealt, log). Each part's share is routed via
        damage_part (so it taps the main per the part's to_main config). The
        total is DIVIDED among parts, never dealt in full to each.

        Modes (default aoe_default_mode): `weighted` (proportional to each
        part's aoe_weight), `uniform` (equal), `fragment` (N discrete
        weighted-random hits — N = fragments or aoe_fragment_count), and
        `main_only` (ignore parts, hit the main HP directly). A target with
        no parts (or weights all zero) takes the full `total` to main.

        Spatial: when `origin` (x, y) + `radius` are given, only parts with a
        cell within `radius` (Chebyshev) of the origin are eligible — a blast
        that doesn't reach the whole body (matters for located + footprint-
        region parts). If the filter leaves no parts, the total goes to main."""
        target = self.entities.get(target_id)
        if target is None:
            raise NotFound(f"Entity '{target_id}' not found.")
        try:
            total = int(total)
        except (TypeError, ValueError):
            raise VTTError("damage_spread: total must be an integer.")
        mode = str(mode) if mode is not None else \
            str(self.rules.get("aoe_default_mode", "weighted"))

        parts = self.entity_parts(target_id)
        if origin is not None and radius is not None:
            ox, oy = int(origin[0]), int(origin[1])
            r = int(radius)
            parts = [p for p in parts
                     if any(max(abs(cx - ox), abs(cy - oy)) <= r
                            for (cx, cy) in self.entity_cells(p))]

        def _hit_main(amount: int) -> Tuple[int, List[str]]:
            """Fallback: deal `amount` straight to the target's main HP."""
            if amount == 0:
                return 0, []
            hp_var, _, _ = target._vital_var_names()
            log = target.write_var(
                hp_var, int(target.vars.get(hp_var, 0) or 0) - amount)
            return amount, (log or [])

        if mode == "main_only" or not parts:
            return _hit_main(total)

        # Per-part share amounts.
        shares: Dict[str, int] = {}
        if mode == "fragment":
            n = int(fragments) if fragments is not None else \
                int(self.rules.get("aoe_fragment_count", 4))
            n = max(1, n)
            weighted = [(p, self._part_aoe_weight(p)) for p in parts]
            wsum = sum(w for _p, w in weighted)
            rng = getattr(self, "_rng", None) or random
            per = total // n
            rem = total - per * n      # give the remainder to the first frags
            for i in range(n):
                amt = per + (1 if i < rem else 0)
                if amt == 0:
                    continue
                if wsum > 0:
                    r = rng.random() * wsum
                    acc = 0.0
                    pick = weighted[-1][0]
                    for p, w in weighted:
                        acc += w
                        if r <= acc:
                            pick = p
                            break
                else:
                    pick = parts[rng.randrange(len(parts))]
                shares[pick.id] = shares.get(pick.id, 0) + amt
        else:
            if mode == "uniform":
                weights = [(p, 1.0) for p in parts]
            else:  # weighted (default / unknown)
                weights = [(p, self._part_aoe_weight(p)) for p in parts]
            wsum = sum(w for _p, w in weights)
            if wsum <= 0:
                return _hit_main(total)
            # Largest-remainder apportionment so the shares sum to `total`.
            raw = [(p, total * w / wsum) for p, w in weights]
            floored = [(p, int(v)) for p, v in raw]
            assigned = sum(v for _p, v in floored)
            leftover = total - assigned
            order = sorted(range(len(raw)),
                           key=lambda i: raw[i][1] - floored[i][1], reverse=True)
            for p, v in floored:
                if v:
                    shares[p.id] = shares.get(p.id, 0) + v
            for i in order[:max(0, leftover)]:
                pid = raw[i][0].id
                shares[pid] = shares.get(pid, 0) + 1

        to_main = 0
        log: List[str] = []
        for pid, amt in shares.items():
            if amt == 0:
                continue
            tm, plog = self.damage_part(pid, amt)
            to_main += tm
            log += plog
        return to_main, log

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
        snap = e.to_dict()
        # A rider that dies falls OFF its vehicle — don't carry the mount
        # links into the corpse, or revive_corpse would restore a phantom-
        # mounted entity (excluded from render/occupancy as a "rider", or
        # silently re-seated onto a still-living vehicle for free). It can
        # always mount again after revival.
        snap.pop("mounted_on", None)
        snap.pop("mount_slot", None)
        corpses[e.id] = {
            "entity": snap,
            "died_round": self.round_number,
        }
        # Snapshot the whole attached part SUBTREE (parts, their parts, ...)
        # alongside the parent so a later revive_corpse restores the whole
        # creature — multi-level limbs included (BFS order = parents before
        # children, so revive can spawn each after its parent). The parts
        # themselves are removed from the match by _process_death. Kept
        # minimal — the "regrow from a fresh template" variant is a deferred
        # TODO.
        part_dicts = [pp.to_dict() for pp in self.entity_part_subtree(e.id)]
        if part_dicts:
            corpses[e.id]["parts"] = part_dicts

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
        # Body parts snapshotted at death (see _store_corpse). Re-spawned
        # after the parent so reviving a creature restores its limbs.
        stored_parts = copy.deepcopy(corpse.get("parts") or [])
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
            # Restore the creature's body parts at the parent's cell. Each
            # snapshot carries its part_of (-> e.id) so it re-attaches; a
            # part already taking that id (somehow still alive) is skipped.
            for pd in stored_parts:
                pdc = copy.deepcopy(pd)
                # A glued part respawns at the parent's cell; a LOCATED part
                # keeps its own snapshotted position.
                located = bool((pdc.get("vars") or {}).get("__part_located"))
                px = int(pdc.get("x", x)) if located else x
                py = int(pdc.get("y", y)) if located else y
                pdc["x"] = px
                pdc["y"] = py
                pid = str(pdc.get("id", ""))
                if not pid or pid in self._taken_entity_ids():
                    continue
                part_e = Entity.from_dict(pdc)
                try:
                    _, plog = part_e.spawn(self, px, py)
                    spawn_log += plog
                except VTTError:
                    # A part that can't be placed (e.g. id clash or its cell
                    # is now occupied) is skipped rather than aborting the
                    # whole revive.
                    pass
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
        # Resume any auras suspended while this entity (or its parts) were dead
        # — done last, once hp is settled and the entity SURVIVED the revive
        # (if the revive policy left it dead, check_death above re-killed it and
        # the auras stay suspended). No-op unless mode='suspend' kept a binding.
        if e.id in self.entities and e.is_alive:
            self._resume_anchored_zones(e.id)
            for part in self.entity_part_subtree(e.id):
                self._resume_anchored_zones(part.id)
        return e.id, spawn_log + revive_log + death_log

    # ---------- polymorph / transform (identity-preserving statblock swap) -----
    # A transform REPLACES an entity's presented statblock — name, vars (incl.
    # actions + footprint), passives, clamps, status, and its attached part
    # subtree — while PRESERVING its identity: id, position, facing, team, and
    # turn-order slot all stay, so references / initiative / allegiance survive.
    # The pre-transform statblock is captured and (optionally) stashed at a
    # caller-chosen var path; reverting reads that path back. Because the stash
    # is an ordinary var the GM names, transforms STACK (stash each to a
    # different path, revert in any order) and the snapshot is inspectable /
    # editable like any other var.

    def capture_statblock(self, e: "Entity") -> Dict[str, Any]:
        """Snapshot `e`'s restorable statblock: name, vars, status, passives,
        clamps, facing, plus the whole attached part SUBTREE as full entity
        dicts (BFS, parents before children). Identity — id, position,
        part/mount links — is NOT captured (it stays on the live entity across
        a transform). The returned dict doubles as a transform template."""
        d = e.to_dict()
        for k in ("id", "x", "y", "part_of", "mounted_on", "mount_slot"):
            d.pop(k, None)
        subtree = self.entity_part_subtree(e.id)
        if subtree:
            d["parts"] = [p.to_dict() for p in subtree]
        return d

    def _apply_statblock_parts(self, parent: "Entity",
                               parts_spec: Any) -> List[str]:
        """Spawn `parts_spec` as `parent`'s body parts. Accepts the two shapes
        the rest of the engine uses: a dict {role: part-template} (single level,
        like a summon template) OR a list of full part entity dicts carrying
        `id`/`part_of` (a captured subtree — multi-level, BFS). Ids are
        re-minted to avoid collisions and `part_of` links are remapped so a
        part-of-a-part re-attaches correctly. Returns spawn log lines."""
        if not parts_spec:
            return []
        name_var = self.part_name_var()
        # Normalize to a list of part dicts. Dict form carries no ids/part_of
        # (each is a direct child of `parent`); record the role as part_name.
        if isinstance(parts_spec, dict):
            part_list = []
            for role, pt in parts_spec.items():
                if not isinstance(pt, dict):
                    continue
                d = copy.deepcopy(pt)
                nv = d.setdefault("vars", {})
                if role is not None and name_var not in nv:
                    nv[name_var] = role
                d.pop("id", None)
                d.pop("part_of", None)
                part_list.append(d)
        elif isinstance(parts_spec, (list, tuple)):
            part_list = [copy.deepcopy(p) for p in parts_spec
                         if isinstance(p, dict)]
        else:
            return []
        if not part_list:
            return []
        # Re-mint ids; remember the mapping so intra-subtree part_of links
        # resolve to the new ids (a child whose parent is also in the list).
        own_ids = {d["id"] for d in part_list if d.get("id")}
        idmap: Dict[str, str] = {}
        for d in part_list:
            base = (d.get("id") or (d.get("vars") or {}).get(name_var)
                    or d.get("name") or f"{parent.id}_part")
            nid = self.mint_entity_id(str(base))
            if d.get("id"):
                idmap[d["id"]] = nid
            d["__nid"] = nid
        # Segment chains reference the segment ahead via the `__follows` var
        # (an entity id); remap it like part_of so a transplanted snake body
        # keeps its chain. A `__follows` pointing at the head resolves to the
        # parent's preserved id (not in idmap) and is left unchanged.
        for d in part_list:
            dv = d.get("vars")
            if isinstance(dv, dict) and dv.get("__follows") in idmap:
                dv["__follows"] = idmap[dv["__follows"]]
        log: List[str] = []
        # Spawn parents before children: a part whose part_of points at another
        # part in this list waits until that part has spawned.
        pending = list(part_list)
        guard = 0
        while pending and guard <= len(part_list):
            guard += 1
            progressed = False
            for d in list(pending):
                po = d.get("part_of")
                if po in own_ids and idmap.get(po) not in self.entities:
                    continue  # its parent part isn't spawned yet
                new_po = idmap[po] if po in own_ids else parent.id
                located = bool((d.get("vars") or {}).get("__part_located"))
                px = int(d.get("x", parent.x)) if located else parent.x
                py = int(d.get("y", parent.y)) if located else parent.y
                spec = {k: v for k, v in d.items() if k not in ("__nid", "parts")}
                spec["id"] = d["__nid"]
                spec["part_of"] = new_po
                spec["x"], spec["y"] = px, py
                if "name" not in spec:
                    spec["name"] = str((spec.get("vars") or {}).get(
                        name_var, spec["id"]))
                try:
                    pe = Entity.from_dict(spec)
                    _, slog = pe.spawn(self, px, py)
                    log += slog
                except VTTError:
                    pass  # malformed part entry — skip, don't abort the swap
                pending.remove(d)
                progressed = True
            if not progressed:
                break
        self._restamp_parts_for(parent.id)
        return log

    def apply_statblock(self, e: "Entity", statblock: Dict[str, Any], *,
                        hp_mode: Optional[str] = None) -> List[str]:
        """Replace `e`'s statblock in place from `statblock` (a
        capture_statblock dict or a summon-style template), PRESERVING e's id,
        position, facing, team, and turn-order slot. HP carries per `hp_mode`
        (default the transform_hp_mode rule: percent | keep | full). The
        entity's current attached parts are despawned (no corpse) and the
        statblock's `parts`/`segments` spawned. Returns log lines."""
        if not isinstance(statblock, dict):
            raise VTTError("transform: statblock must be a dict.")
        sb = copy.deepcopy(statblock)
        # Vehicle riders: if the entity currently carries riders, the new form
        # must still have a slot for each, or the riders would be orphaned
        # (flagged mounted to a slot-less form). If every rider's slot exists
        # in the new form they stay mounted; otherwise the
        # transform_rider_mismatch_mode rule decides: 'block' (refuse, raise
        # before any change) or 'eject' (dismount all riders, then transform).
        riders = self.vehicle_riders(e.id)
        if riders:
            new_slots = (sb.get("vars") or {}).get("slots")
            new_slots = new_slots if isinstance(new_slots, dict) else {}
            missing = sorted({r.mount_slot for r in riders
                              if r.mount_slot not in new_slots})
            if missing:
                mode = str(self.rules.get("transform_rider_mismatch_mode", "block"))
                if mode == "eject":
                    self._release_riders(e.id, mode="eject")
                else:
                    raise VTTError(
                        f"transform: new form lacks slot(s) "
                        f"{', '.join(repr(s) for s in missing)} for current "
                        f"rider(s) — dismount them first (or set "
                        f"transform_rider_mismatch_mode=eject).")
        hp_var, max_hp_var, turnorder_var = e._vital_var_names()
        team_var = str(self.rules.get("team_var", "team"))
        if hp_mode is None:
            hp_mode = str(self.rules.get("transform_hp_mode", "percent"))
        old_hp = e.vars.get(hp_var)
        old_max = e.vars.get(max_hp_var)
        old_team = e.vars.get(team_var)
        old_init = e.vars.get(turnorder_var)
        # Despawn current attached parts (children first). This is a despawn,
        # not a death — no corpse, no on_death.
        for p in reversed(self.entity_part_subtree(e.id)):
            if p.id in self.entities:
                p.remove()
        # Swap the presented fields. Death checks are suppressed across the
        # swap window: an intermediate var state (e.g. new max_hp before hp is
        # set) could momentarily satisfy the death condition.
        new_vars = copy.deepcopy(sb.get("vars") or {})
        new_max = new_vars.get(max_hp_var)
        target_hp = new_vars.get(hp_var)
        try:
            if (hp_mode == "percent" and isinstance(new_max, (int, float))
                    and old_max not in (None, 0)):
                frac = float(old_hp) / float(old_max)
                scaled = new_max * frac
                target_hp = int(round(scaled)) if isinstance(new_max, int) else scaled
            elif (hp_mode == "keep" and isinstance(new_max, (int, float))
                  and old_hp is not None):
                target_hp = min(float(old_hp), float(new_max))
                if isinstance(new_max, int):
                    target_hp = int(target_hp)
            # 'full' (and any fallthrough) leaves target_hp = statblock's value
        except (TypeError, ValueError):
            pass
        if target_hp is not None:
            new_vars[hp_var] = target_hp
        # Preserve team membership + turn-order slot (identity), overriding
        # whatever the statblock carried. The new form keeps its allegiance
        # and initiative; a GM who wants a slow form sets it explicitly after.
        if old_team is not None:
            new_vars[team_var] = old_team
        if old_init is not None:
            new_vars[turnorder_var] = old_init
        self._death_check_suppressed_ids.add(e.id)
        log: List[str] = []
        try:
            e.vars = new_vars
            e.name = sb.get("name", e.name)
            e.passives = {pid: Passive.from_dict(pd)
                          for pid, pd in (sb.get("passives") or {}).items()}
            e.clamps = {path: ClampSpec.from_dict(cd)
                        for path, cd in (sb.get("clamps") or {}).items()}
            e.status = copy.deepcopy(sb.get("status") or {})
            # facing is identity — preserved (not taken from the statblock).
            self._turn_order_dirty = True
            self._rebuild_turn_order()
            self._restamp_anchors_for(e.id)
            log += self._apply_statblock_parts(e, sb.get("parts"))
            seg = sb.get("segments")
            if seg:
                log += self._apply_statblock_parts(e, seg)
        finally:
            self._death_check_suppressed_ids.discard(e.id)
        if e.id in self.entities:
            log += self.check_death(e.id)
        return log

    def transform_entity(self, eid: str, template: Any,
                         stash_path: Optional[str] = None,
                         *, hp_mode: Optional[str] = None) -> List[str]:
        """Transform `eid` into `template` (a statblock dict). If `stash_path`
        is given, the PRE-transform statblock is stored at
        entity[eid].vars.<stash_path> (in the NEW form's vars, so it survives),
        and `revert(eid, stash_path)` restores it. Stash paths are ordinary
        vars chosen by the caller, so transforms stack."""
        e = self.entities.get(eid)
        if e is None:
            raise NotFound(f"Entity '{eid}' not found.")
        if e.is_part:
            raise VTTError(
                f"'{eid}' is a body part — transform its parent instead.")
        snap = self.capture_statblock(e) if stash_path else None
        log = self.apply_statblock(e, template, hp_mode=hp_mode)
        if stash_path and e.id in self.entities:
            e.write_var(str(stash_path), snap)
        return log

    def revert_entity(self, eid: str, stash_path: str,
                      *, hp_mode: Optional[str] = None) -> List[str]:
        """Restore `eid`'s statblock from the snapshot previously stashed at
        entity[eid].vars.<stash_path> by transform_entity. The stash var
        naturally disappears (vars are replaced by the older snapshot, which
        predates the stash)."""
        e = self.entities.get(eid)
        if e is None:
            raise NotFound(f"Entity '{eid}' not found.")
        # Walk the dotted stash path through e's vars.
        node: Any = e.vars
        for key in str(stash_path).split("."):
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                node = None
                break
        snap = node
        if not isinstance(snap, dict):
            raise VTTError(
                f"revert: no stashed statblock at `{stash_path}` on `{eid}`.")
        return self.apply_statblock(e, snap, hp_mode=hp_mode)

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

    def _firing_passives(self, target_eid):
        """Passives that fire for an event on `target_eid` (self = target):
        the match's global passives PLUS the target entity's team passives.
        Entity-owned passives are iterated separately at each fire site."""
        yield from self.global_passives.values()
        e = self.entities.get(target_eid) if target_eid else None
        if e is not None and e.team is not None:
            tp = self.team_passives.get(e.team)
            if tp:
                yield from tp.values()

    def fire_hook(self, when: str, *, target_ids: Optional[List[str]] = None,
                  own_only_targets: Optional[List[str]] = None,
                  extras: Optional[Dict[str, Any]] = None) -> List[str]:
        """
        Fire every passive matching `when` for each target entity.

        target_ids defaults to entities currently in `turn_order` (alive and
        with initiative). For each target, fires global passives first (in
        insertion order), then the target's own entity passives (in insertion
        order). Only passives whose own `when` matches are run.

        own_only_targets (if given) fire ONLY their entity-owned passives —
        no global/team passives. This is how an attached part rides its
        parent's turn/round clock: the part runs its own on_turn_*/
        on_round_* passives, but match-wide globals (which already fired
        once per acting unit) are NOT re-run per part.

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
            ctx = EvalCtx(this=this_id, target=tid, extras=extras)
            # Globals first (in insertion order).
            for p in self._firing_passives(tid):
                if p.when != when:
                    continue
                log.append(_run_passive_safely(engine, p, ctx, target_id=tid, is_global=True))
            # A global/team passive may have removed this entity; don't fire
            # its own passives afterward.
            if tid not in self.entities:
                continue
            # Then entity-owned passives.
            for pid, p in list(e.passives.items()):
                if p.when != when:
                    continue
                log.append(_run_passive_safely(engine, p, ctx, target_id=tid, is_global=False))
        # Attached parts riding the parent's clock: own passives only.
        for tid in (own_only_targets or ()):
            e = self.entities.get(tid)
            if e is None:
                continue
            ctx = EvalCtx(this=this_id, target=tid, extras=extras)
            for pid, p in list(e.passives.items()):
                if p.when != when:
                    continue
                log.append(_run_passive_safely(engine, p, ctx, target_id=tid, is_global=False))
        return log

    def emit_event(self, name: str, payload: Optional[Dict[str, Any]] = None,
                   target: Optional[str] = None) -> List[str]:
        """Fire the custom event `name` — the GM-extensible event bus. Runs
        every passive whose `when` is 'event:<name>':
          - GLOBAL passives fire ONCE (self = `target` if given, else the
            current-turn entity, else None): match-level decoupled handlers.
          - When `target` is given, the target's TEAM passives + its OWN
            passives also fire (self = target): a DIRECTED event.
        For a broadcast to many entities the GM loops emit per target (cause
        and effect stay explicit). `payload` (a dict) is readable inside a
        handler via event_get / event_has; `event_name` is bound. Re-entrancy
        (a handler that emits) is capped by the event_recursion_limit rule.
        Returns one log line per fired handler."""
        when = _EVENT_WHEN_PREFIX + str(name)
        limit = int(self.rules.get("event_recursion_limit", 64))
        top = self._event_depth == 0
        if self._event_depth >= limit:
            # Suppress (don't fire) and latch a single warning for the chain;
            # the top-level emit drains it so it reaches the caller.
            if not self._event_warned:
                self._event_warned = True
                self._event_warnings.append(
                    f"⚠️ event recursion limit ({limit}) reached emitting "
                    f"`{name}`; suppressing further emits in this chain.")
            return []
        from formula import FormulaEngine, EvalCtx
        engine = FormulaEngine(self)
        this_id = self.current_entity_id()
        self._event_depth += 1
        self._event_stack.append(dict(payload or {}))
        log: List[str] = []
        try:
            gself = target if target else this_id
            extras: Dict[str, Any] = {"event_name": str(name), "hook_name": when}
            if target is not None:
                extras["target"] = target
            gctx = EvalCtx(this=this_id, target=gself, extras=extras)
            # Global handlers fire once.
            for p in self.global_passives.values():
                if p.when == when:
                    log.append(_run_passive_safely(
                        engine, p, gctx, target_id=gself, is_global=True))
            # Directed: the target's team + own handlers (self = target).
            if target is not None:
                e = self.entities.get(target)
                if e is not None:
                    tctx = EvalCtx(this=this_id, target=target, extras=extras)
                    if e.team is not None:
                        for p in (self.team_passives.get(e.team) or {}).values():
                            if p.when == when:
                                log.append(_run_passive_safely(
                                    engine, p, tctx, target_id=target,
                                    is_global=True))
                    # A team handler may have removed the target; don't fire
                    # its own handlers afterward.
                    if target in self.entities:
                        for p in list(e.passives.values()):
                            if p.when == when:
                                log.append(_run_passive_safely(
                                    engine, p, tctx, target_id=target,
                                    is_global=False))
        finally:
            self._event_stack.pop()
            self._event_depth -= 1
        if top:
            # Back at the outermost emit: surface any buffered recursion
            # warning and reset the latch for the next chain.
            if self._event_warnings:
                log = log + self._event_warnings
                self._event_warnings = []
            self._event_warned = False
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
        for p in self._firing_passives(entity_id):
            if p.when == kind_hook:
                _maybe_fire(p, is_global=True)
        # A global/team handler may have removed the entity; don't fire its
        # own passives from beyond the grave (mirrors fire_status_event).
        if entity_id in self.entities:
            for p in e.passives.values():
                if p.when == kind_hook:
                    _maybe_fire(p, is_global=False)

        # Wave 2: on_var_written catch-all passives
        for p in self._firing_passives(entity_id):
            if p.when == "on_var_written":
                _maybe_fire(p, is_global=True)
        if entity_id in self.entities:
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
        for p in self._firing_passives(entity_id):
            if p.when == "on_var_write_attempt":
                _maybe_fire(p, is_global=True)
        # A global/team handler may have removed the entity mid-fire; don't
        # fire its own passives from beyond the grave.
        if entity_id in self.entities:
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

    def _apply_default_vars(self, entity: "Entity") -> None:
        """Fill `entity`'s MISSING vars from the system's
        `default_entity_vars` rule (dotted-path -> default value). Called
        from Entity.spawn for every creation path, at the very start —
        before vital-var validation — so a default can satisfy a required
        var (e.g. hp). Only sets a path the entity doesn't already have:
        an `!ent add` value, a summon-template value, or a revive
        snapshot's value all win over the default (injection, not
        enforcement). The value is deep-copied so a mutable default
        (dict/list) isn't shared across entities. Malformed paths /
        non-dict rule are skipped silently (same convention as the other
        spawn-time defaulters)."""
        self._fill_missing_vars(entity, self.rules.get("default_entity_vars"))

    def _fill_missing_vars(self, entity: "Entity", defaults: Any) -> None:
        """Fill `entity`'s MISSING vars from a {dotted-path -> value} dict
        (injection, not enforcement: a path the entity already has is kept).
        Values are deep-copied; malformed paths / non-dict input skipped.
        Shared by default_entity_vars and the split-head template stamp."""
        if not isinstance(defaults, dict):
            return
        for path, value in defaults.items():
            if not isinstance(path, str) or not path:
                continue
            segs = path.split(".")
            # Walk to the parent dict, treating any non-dict encountered
            # along the way as "absent" (don't clobber a scalar that a
            # shorter path already placed).
            cur = entity.vars
            present = True
            for seg in segs[:-1]:
                nxt = cur.get(seg) if isinstance(cur, dict) else None
                if not isinstance(nxt, dict):
                    present = False
                    break
                cur = nxt
            leaf = segs[-1]
            if present and isinstance(cur, dict) and leaf in cur:
                continue  # entity already has this var — keep it
            # Materialize intermediate dicts, then set the leaf.
            cur = entity.vars
            for seg in segs[:-1]:
                nxt = cur.get(seg)
                if not isinstance(nxt, dict):
                    nxt = {}
                    cur[seg] = nxt
                cur = nxt
            cur[leaf] = copy.deepcopy(value)

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
            # deepcopy each tile's data: tile_set_path / the tile_set primitive
            # mutate tile dicts IN PLACE, so a by-reference snapshot would be
            # corrupted by a later edit (defeating undo change-detection and
            # action rollback — the same defect fixed for entity vars).
            "tiles": {
                f"{x},{y}": copy.deepcopy(dat)
                for (x, y), dat in sorted(self.tiles.items())
            },
            "zones": {
                name: self._zone_to_dict(z)
                for name, z in sorted(self.zones.items())
            },
            "tile_templates": {
                name: tpl.to_dict()
                for name, tpl in sorted(self.tile_templates.items())
            },
            "status_definitions": {
                name: copy.deepcopy(d)
                for name, d in sorted(self.status_definitions.items())
            },
            "formula_functions": {
                name: fn.to_dict()
                for name, fn in sorted(self.formula_functions.items())
            },
            "aliases": dict(self.aliases),
            "macros": dict(self.macros),
            "tables": dict(self.tables),
            "watchers": copy.deepcopy(self.watchers),
            "hidden_layers": sorted(self.hidden_layers),
            "outcome": copy.deepcopy(self.outcome),
            "team_data": copy.deepcopy(self.team_data),
            "team_passives": {
                team: {pid: p.to_dict() for pid, p in d.items()}
                for team, d in self.team_passives.items()
            },
            "vars": copy.deepcopy(self.vars),
            "scheduled": copy.deepcopy(self.scheduled),
            "event_log": copy.deepcopy(self.event_log),
            "owner": self.owner,
            "cohosts": list(self.cohosts),
            "access_overrides": dict(self.access_overrides),
            "bound_channels": copy.deepcopy(self.bound_channels),
            "fog_enabled": bool(self.fog_enabled),
            "fog_memory": bool(self.fog_memory),
            # sets/tuples aren't JSON-able; store as {team: [[x, y], ...]}
            "explored": {
                team: sorted([x, y] for (x, y) in cells)
                for team, cells in self.explored.items()
            },
            "fog_reveals": {
                team: [{"cells": sorted([x, y] for (x, y) in r.get("cells", ())),
                        "until": r.get("until")}
                       for r in recs]
                for team, recs in self.fog_reveals.items()
            },
            "team_colors": dict(self.team_colors),
            "color_enabled": bool(self.color_enabled),
            "channel_views": {k: [int(v[0]), int(v[1])]
                              for k, v in self.channel_views.items()
                              if isinstance(v, (list, tuple)) and len(v) == 2},
            "map_legend_enabled": bool(self.map_legend_enabled),
            "background": copy.deepcopy(self.background),
            "border_show": self.border_show,
            "border_color": self.border_color,
            "border_opacity": self.border_opacity,
            "render_mode": self.render_mode,
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
                # deepcopy: a snapshot's tile dict is loaded here; reusing it
                # by reference would re-share it with the retained snapshot, so
                # a later in-place !tile set would corrupt undo/rollback (the
                # load-side twin of the to_dict deepcopy above).
                m.tiles[(x, y)] = copy.deepcopy(val)
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
        raw_status_defs = d.get("status_definitions", {}) or {}
        m.status_definitions = {}
        for sname, sdef in raw_status_defs.items():
            if isinstance(sname, str) and isinstance(sdef, dict):
                m.status_definitions[sname] = copy.deepcopy(sdef)
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
        raw_macros = d.get("macros", {}) or {}
        m.macros = {
            str(k): str(v) for k, v in raw_macros.items()
            if isinstance(k, str) and isinstance(v, str)
        }
        raw_tables = d.get("tables", {}) or {}
        m.tables = {
            str(k): str(v) for k, v in raw_tables.items()
            if isinstance(k, str) and isinstance(v, str)
        }
        raw_watch = d.get("watchers", {}) or {}
        m.watchers = {
            str(k): copy.deepcopy(v) for k, v in raw_watch.items()
            if isinstance(k, str) and isinstance(v, dict)
        } if isinstance(raw_watch, dict) else {}
        raw_layers = d.get("hidden_layers", []) or []
        m.hidden_layers = {str(x) for x in raw_layers
                           if isinstance(x, str)} if isinstance(raw_layers, (list, tuple, set)) else set()
        raw_outcome = d.get("outcome")
        m.outcome = copy.deepcopy(raw_outcome) if isinstance(raw_outcome, dict) else None
        raw_td = d.get("team_data", {}) or {}
        m.team_data = {
            str(k): copy.deepcopy(v) for k, v in raw_td.items()
            if isinstance(k, str) and isinstance(v, dict)
        } if isinstance(raw_td, dict) else {}
        raw_tp = d.get("team_passives", {}) or {}
        m.team_passives = {}
        if isinstance(raw_tp, dict):
            for team, pd in raw_tp.items():
                if not isinstance(team, str) or not isinstance(pd, dict):
                    continue
                m.team_passives[team] = {
                    str(pid): Passive.from_dict(spec)
                    for pid, spec in pd.items() if isinstance(spec, dict)
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
            k: (copy.deepcopy(v) if isinstance(v, dict) else {})
            for k, v in raw_bound.items() if isinstance(k, str)
        } if isinstance(raw_bound, dict) else {}
        m.fog_enabled = bool(d.get("fog_enabled", False))
        m.fog_memory = bool(d.get("fog_memory", False))
        raw_expl = d.get("explored", {})
        m.explored = {
            team: {(int(c[0]), int(c[1])) for c in cells
                   if isinstance(c, (list, tuple)) and len(c) == 2}
            for team, cells in raw_expl.items()
            if isinstance(team, str) and isinstance(cells, list)
        } if isinstance(raw_expl, dict) else {}
        raw_rev = d.get("fog_reveals", {})
        m.fog_reveals = {
            team: [
                {"cells": {(int(c[0]), int(c[1])) for c in r.get("cells", [])
                           if isinstance(c, (list, tuple)) and len(c) == 2},
                 "until": (int(r["until"]) if r.get("until") is not None else None)}
                for r in recs if isinstance(r, dict)
            ]
            for team, recs in raw_rev.items()
            if isinstance(team, str) and isinstance(recs, list)
        } if isinstance(raw_rev, dict) else {}
        raw_tc = d.get("team_colors", {})
        m.team_colors = {
            str(k): str(v) for k, v in raw_tc.items()
            if isinstance(k, str) and isinstance(v, str)
        } if isinstance(raw_tc, dict) else {}
        m.color_enabled = bool(d.get("color_enabled", True))
        raw_views = d.get("channel_views", {})
        m.channel_views = {
            str(k): [int(v[0]), int(v[1])] for k, v in raw_views.items()
            if isinstance(k, str) and isinstance(v, (list, tuple)) and len(v) == 2
        } if isinstance(raw_views, dict) else {}
        m.map_legend_enabled = bool(d.get("map_legend_enabled", False))
        raw_bg = d.get("background")
        m.background = copy.deepcopy(raw_bg) if isinstance(raw_bg, dict) else None
        bs = d.get("border_show")
        m.border_show = bool(bs) if isinstance(bs, bool) else None
        bcol = d.get("border_color")
        m.border_color = bcol if isinstance(bcol, str) and bcol else None
        bop = d.get("border_opacity")
        m.border_opacity = int(bop) if isinstance(bop, (int, float)) \
            and not isinstance(bop, bool) else None
        rm = d.get("render_mode")
        m.render_mode = rm if rm in ("text", "image") else "text"
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
    def _effective_disguise(self, e: "Entity",
                            pov_team: Optional[str]) -> Optional[Dict[str, Any]]:
        """The fake statblock `e` PRESENTS to a viewer on team `pov_team`, or
        None to show the truth. Display-only and POV-gated: a disguise applies
        only to a non-omniscient viewer (pov_team is not None) who is NOT on
        the entity's own team — allies and the omniscient/GM view always see
        the real entity. The disguise lives in the var named by the
        `disguise_var` rule (default `disguise`): a dict {name?, glyph?,
        glyphs?, color?, vars?: {...}}. Engine MECHANICS (targeting, formulas,
        damage) always use the real statblock; only the rendered name / glyph /
        color / roster stats are swapped. (116 fake-statblock; decoy/illusion
        is a GM composition on top of it.)"""
        if pov_team is None:
            return None
        var = str(self.rules.get("disguise_var", "disguise"))
        d = e.vars.get(var)
        if not isinstance(d, dict) or not d:
            return None
        if e.team is not None and str(e.team) == str(pov_team):
            return None
        return d

    def entity_glyph(self, e: "Entity",
                     pov_team: Optional[str] = None) -> str:
        """The map symbol for `e`: a per-facing `glyphs.<facing>` var wins,
        else a direction-agnostic single `glyph` var, else the default
        DIRECTION_ARROWS arrow for its facing (`@` if unknown). Custom
        glyphs must be exactly one character (alignment); anything else is
        ignored and falls through — same rule as tile/zone glyphs. When `e`
        is disguised to `pov_team` (116), the disguise's glyph/glyphs are
        resolved first; a disguise without a glyph falls through to the real
        resolution."""
        facing = getattr(e, "facing", "")
        dis = self._effective_disguise(e, pov_team)
        if dis is not None:
            dglyphs = dis.get("glyphs")
            if isinstance(dglyphs, dict):
                dg = dglyphs.get(facing)
                if isinstance(dg, str) and len(dg) == 1:
                    return dg
            dg = dis.get("glyph")
            if isinstance(dg, str) and len(dg) == 1:
                return dg
        glyphs = e.vars.get("glyphs")
        if isinstance(glyphs, dict):
            g = glyphs.get(facing)
            if isinstance(g, str) and len(g) == 1:
                return g
        g = e.vars.get("glyph")
        if isinstance(g, str) and len(g) == 1:
            return g
        return DIRECTION_ARROWS.get(facing, "@")

    def entity_has_custom_glyph(self, e: "Entity") -> bool:
        """True when `e` has a valid custom glyph (per-facing `glyphs` or the
        single `glyph` var) rather than the default facing arrow — drives
        the part-over-parent render priority."""
        gd = e.vars.get("glyphs")
        if isinstance(gd, dict):
            gg = gd.get(getattr(e, "facing", ""))
            if isinstance(gg, str) and len(gg) == 1:
                return True
        g = e.vars.get("glyph")
        return isinstance(g, str) and len(g) == 1

    def entity_color(self, e: "Entity",
                     pov_team: Optional[str] = None) -> Optional[str]:
        """The resolved color NAME for `e` (or None): its `color` var wins,
        else its team's color — the match team_colors map, defaulting to
        the team's own name when that name is itself a palette color (so a
        team literally named 'red' renders red). Unknown names -> None. When
        `e` is disguised to `pov_team` (116), the disguise's `color` wins."""
        dis = self._effective_disguise(e, pov_team)
        if dis is not None:
            dc = dis.get("color")
            if isinstance(dc, str) and dc in TEXT_COLORS:
                return dc
        c = e.vars.get("color")
        if isinstance(c, str) and c in TEXT_COLORS:
            return c
        team = e.team
        if team:
            tc = self.team_colors.get(team)
            if isinstance(tc, str) and tc in TEXT_COLORS:
                return tc
            if team in TEXT_COLORS:
                return team
        return None

    def entity_display_name(self, e: "Entity",
                            pov_team: Optional[str] = None) -> str:
        """`e`'s presented name: the disguise's `name` when disguised to
        `pov_team` (116), else the real name (falling back to id)."""
        dis = self._effective_disguise(e, pov_team)
        if dis is not None:
            n = dis.get("name")
            if isinstance(n, str) and n:
                return n
        return e.name or e.id

    def _resolve_color_value(self, val: Any,
                             extras: Dict[str, Any]) -> Optional[str]:
        """Resolve a tile/zone `color` data value to a palette NAME or None.
        A bare TEXT_COLORS name is a literal; anything else is treated as a
        formula EXPRESSION (evaluated with `extras` bound — tile_x/tile_y
        for tiles, zone_name for zones) whose string result must itself be
        a palette name. Malformed / non-palette results -> None (no color),
        the same fail-safe stance as the visibility/block conditions."""
        if val in (None, ""):
            return None
        if not isinstance(val, str):
            val = str(val)
        if val in TEXT_COLORS:
            return val
        from formula import FormulaEngine, EvalCtx, FormulaError
        try:
            res = FormulaEngine(self).eval_expression(
                val, EvalCtx(this=self.current_entity_id(), extras=dict(extras)))
        except FormulaError:
            return None
        res = str(res) if res is not None else ""
        return res if res in TEXT_COLORS else None

    def tile_color(self, x: int, y: int) -> Optional[str]:
        """The resolved color name for the tile at (x, y): its instance
        `color` data wins, else its template's `color`. Literal-or-formula
        (see _resolve_color_value). None when unset/unresolved."""
        cell = self.tiles.get((x, y))
        if not isinstance(cell, dict):
            return None
        val = cell.get("color")
        if val in (None, ""):
            tpl_name = cell.get("_template")
            if isinstance(tpl_name, str):
                tpl = self.tile_templates.get(tpl_name)
                if tpl is not None:
                    val = tpl.data.get("color")
        return self._resolve_color_value(val, {"tile_x": x, "tile_y": y})

    def zone_color(self, name: str) -> Optional[str]:
        """The resolved color name for zone `name` (its `color` field,
        literal-or-formula), or None."""
        z = self.zones.get(name)
        if not isinstance(z, dict):
            return None
        return self._resolve_color_value(z.get("color"), {"zone_name": name})

    # ---- sprite resolution (graphics render model) ------------------
    # Sprites are KEY strings stored in data, mirroring glyphs: a per-facing
    # `sprites.<facing>` var > a direction-agnostic `sprite` var > (entity)
    # facing-mirror of an existing facing > the fallback_sprite rule. The
    # engine returns keys + flip flags; the surface maps keys to images.
    _MIRROR_H = {"left": "right", "right": "left",
                 "up_left": "up_right", "up_right": "up_left",
                 "down_left": "down_right", "down_right": "down_left"}
    _MIRROR_V = {"up": "down", "down": "up",
                 "up_left": "down_left", "down_left": "up_left",
                 "up_right": "down_right", "down_right": "up_right"}

    def _sprite_mirror_axes(self, e: "Entity") -> frozenset:
        """Which mirror axes ('h'/'v') are allowed when filling a missing
        facing sprite for `e`: the `sprite_mirror` var > the rule."""
        val = e.vars.get("sprite_mirror")
        if not (isinstance(val, str) and val.strip()):
            val = str(self.rules.get("sprite_mirror", "horizontal"))
        val = val.strip().lower()
        return {"none": frozenset(), "horizontal": frozenset({"h"}),
                "vertical": frozenset({"v"}),
                "both": frozenset({"h", "v"})}.get(val, frozenset({"h"}))

    def _mirror_sprite(self, e: "Entity", sprites: Dict[str, Any],
                       facing: str) -> Optional[Tuple[str, bool, bool]]:
        """Find a mirror of an existing facing in `sprites` to stand in for
        the missing `facing`, per the entity's allowed mirror axes. Returns
        (key, flip_h, flip_v) or None."""
        axes = self._sprite_mirror_axes(e)
        if "h" in axes:
            p = self._MIRROR_H.get(facing)
            k = sprites.get(p) if p else None
            if isinstance(k, str) and k:
                return (k, True, False)
        if "v" in axes:
            p = self._MIRROR_V.get(facing)
            k = sprites.get(p) if p else None
            if isinstance(k, str) and k:
                return (k, False, True)
        return None

    def entity_sprite(self, e: "Entity", pov_team: Optional[str] = None
                      ) -> Optional[Tuple[str, bool, bool]]:
        """The sprite for `e` as (key, flip_h, flip_v), or None when nothing
        resolves (the surface then renders the glyph as text). Resolution
        mirrors entity_glyph: disguise sprites (when disguised to pov_team)
        are tried first, then the entity's own — within each source: exact
        `sprites.<facing>`, then a facing-mirror, then the base `sprite`."""
        facing = getattr(e, "facing", "")
        sources: List[Dict[str, Any]] = []
        dis = self._effective_disguise(e, pov_team)
        if isinstance(dis, dict):
            sources.append(dis)
        sources.append(e.vars)
        for src in sources:
            sprites = src.get("sprites")
            if isinstance(sprites, dict):
                k = sprites.get(facing)
                if isinstance(k, str) and k:
                    return (k, False, False)
                m = self._mirror_sprite(e, sprites, facing)
                if m is not None:
                    return m
            k = src.get("sprite")
            if isinstance(k, str) and k:
                return (k, False, False)
        fb = str(self.rules.get("fallback_sprite", "")).strip()
        return (fb, False, False) if fb else None

    def entity_sprite_mode(self, e: "Entity") -> str:
        """How a multi-tile entity's sprite fills its footprint: the
        `sprite_mode` var > the rule (single | stretch | tile)."""
        val = e.vars.get("sprite_mode")
        if not (isinstance(val, str) and val in ("single", "stretch", "tile")):
            val = str(self.rules.get("sprite_mode", "single"))
        return val if val in ("single", "stretch", "tile") else "single"

    # ---- overlay sprites (status FX + entity overlay var) ----
    @staticmethod
    def _overlay_num(v: Any) -> Optional[int]:
        if isinstance(v, bool) or v is None:
            return None
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str):
            try:
                return int(float(v.strip()))
            except (ValueError, AttributeError):
                return None
        return None

    def _overlay_fields(self, sprite: Any, opacity: Any, tint: Any,
                        layer: Any, default_layer: int) -> Optional[Dict[str, Any]]:
        """Build a normalized overlay placement record from raw values, or
        None when there's no usable sprite key."""
        if not (isinstance(sprite, str) and sprite.strip()):
            return None
        op = self._overlay_num(opacity)
        op = 100 if op is None else max(0, min(100, op))
        t = tint if isinstance(tint, str) and tint.strip() else None
        return {"sprite": sprite.strip(), "opacity": op, "tint": t,
                "layer": self._coerce_layer(layer, default_layer)}

    def _overlay_from_record(self, val: Any,
                             default_layer: int) -> Optional[Dict[str, Any]]:
        """An overlay record from the entity overlay var: a bare sprite-key
        string, or a dict with `sprite` (+ optional opacity/tint/layer; both
        the bare and `sprite_`-prefixed key names are accepted)."""
        if isinstance(val, str):
            return self._overlay_fields(val, None, None, None, default_layer)
        if isinstance(val, dict):
            return self._overlay_fields(
                val.get("sprite"),
                val.get("opacity", val.get("sprite_opacity")),
                val.get("tint", val.get("sprite_tint")),
                val.get("layer", val.get("sprite_layer")),
                default_layer)
        return None

    def entity_overlays(self, e: "Entity",
                        pov_team: Optional[str] = None) -> List[Dict[str, Any]]:
        """Overlay sprite records {sprite, opacity, tint, layer} drawn OVER an
        entity: its active STATUSES (a status definition's `sprite`, overridden
        by the same field on the entity's status instance) plus the entity's
        overlay var (overlay_var rule). Empty if none resolve. A DISGUISED
        entity (shown a disguise to pov_team) shows no overlays so its real
        status FX don't leak through the decoy. Graphics-only (no ASCII)."""
        if self._effective_disguise(e, pov_team) is not None:
            return []
        out: List[Dict[str, Any]] = []
        default_layer = self._layer_rule("sprite_layer_overlay", 150)
        statuses = e.status if isinstance(e.status, dict) else {}
        for sname in sorted(statuses):
            inst = statuses.get(sname)
            inst = inst if isinstance(inst, dict) else {}
            sdef = self.status_definitions.get(sname) or {}

            def pick(key: str):
                v = inst.get(key)
                return v if v is not None and v != "" else sdef.get(key)
            rec = self._overlay_fields(
                pick("sprite"), pick("sprite_opacity"),
                pick("sprite_tint"), pick("sprite_layer"), default_layer)
            if rec:
                out.append(rec)
        ovar = str(self.rules.get("overlay_var", "overlays")) or "overlays"
        bag = e.vars.get(ovar)
        if isinstance(bag, dict):
            for k in sorted(bag):
                rec = self._overlay_from_record(bag.get(k), default_layer)
                if rec:
                    out.append(rec)
        return out

    def tile_sprite(self, x: int, y: int) -> Optional[str]:
        """The sprite key for the tile at (x, y): instance `sprite` data >
        the template's `sprite`. None when unset."""
        cell = self.tiles.get((x, y))
        if not isinstance(cell, dict):
            return None
        k = cell.get("sprite")
        if isinstance(k, str) and k:
            return k
        tpl_name = cell.get("_template")
        if isinstance(tpl_name, str):
            tpl = self.tile_templates.get(tpl_name)
            if tpl is not None:
                tk = tpl.data.get("sprite")
                if isinstance(tk, str) and tk:
                    return tk
        return None

    def zone_sprite(self, name: str) -> Optional[str]:
        """The sprite key for zone `name` (its `sprite` field), or None."""
        z = self.zones.get(name)
        if not isinstance(z, dict):
            return None
        k = z.get("sprite")
        return k if isinstance(k, str) and k else None

    def background_layer(self) -> Optional[Dict[str, Any]]:
        """The resolved background as {sprite, mode}, or None: the per-match
        `background` field > the background_sprite rule (+ background_mode)."""
        bg = self.background
        if isinstance(bg, dict):
            key = bg.get("sprite")
            if isinstance(key, str) and key:
                mode = bg.get("mode")
                return {"sprite": key,
                        "mode": mode if mode in ("stretch", "tile", "center")
                        else "stretch"}
        key = str(self.rules.get("background_sprite", "")).strip()
        if key:
            mode = str(self.rules.get("background_mode", "stretch"))
            return {"sprite": key,
                    "mode": mode if mode in ("stretch", "tile", "center")
                    else "stretch"}
        return None

    def tile_glyph(self, x: int, y: int) -> Optional[str]:
        """The glyph for the tile at (x, y): instance > template (mirrors the
        inline resolution in _render_ascii_impl). None when unset."""
        cell = self.tiles.get((x, y))
        if not isinstance(cell, dict):
            return None
        g = cell.get("glyph")
        if isinstance(g, str) and len(g) == 1:
            return g
        tpl_name = cell.get("_template")
        if isinstance(tpl_name, str):
            tpl = self.tile_templates.get(tpl_name)
            if tpl is not None:
                tg = tpl.data.get("glyph")
                if isinstance(tg, str) and len(tg) == 1:
                    return tg
        return None

    def entity_has_custom_sprite(self, e: "Entity") -> bool:
        """True when `e` has a custom sprite (per-facing `sprites` or the base
        `sprite` var) — the sprite analog of entity_has_custom_glyph, for the
        region-part-over-parent render priority."""
        sd = e.vars.get("sprites")
        if isinstance(sd, dict):
            s = sd.get(getattr(e, "facing", ""))
            if isinstance(s, str) and s:
                return True
        s = e.vars.get("sprite")
        return isinstance(s, str) and bool(s)

    def corpse_sprite(self, corpse: Dict[str, Any]) -> Optional[str]:
        """The sprite key for a corpse, read from its frozen snapshot vars:
        `sprites.<facing>` > `sprite`. No mirror/fallback (a corpse is static
        and undisguised). None when the dead entity had no sprite."""
        ent = corpse.get("entity") if isinstance(corpse, dict) else None
        if not isinstance(ent, dict):
            return None
        vars_ = ent.get("vars") or {}
        sd = vars_.get("sprites")
        if isinstance(sd, dict):
            k = sd.get(ent.get("facing", ""))
            if isinstance(k, str) and k:
                return k
        k = vars_.get("sprite")
        return k if isinstance(k, str) and k else None

    # ---- map viewport (panning) -------------------------------------
    # The viewport caps how much grid renders at once. It engages when
    # EITHER dimension exceeds its cap (so a 70x5 grid still windows
    # horizontally). The window size is min(cap, grid) per axis; the
    # per-channel offset (channel_views[ch]) is the top-left anchor,
    # clamped so the window never runs off the grid.

    def _viewport_dims(self) -> Tuple[int, int]:
        """The window size (w, h) = min(cap, grid) per axis."""
        cw = max(1, int(self.rules.get("viewport_width", 30)))
        ch = max(1, int(self.rules.get("viewport_height", 30)))
        return min(cw, self.grid_width), min(ch, self.grid_height)

    def viewport_engaged(self) -> bool:
        """True iff the grid exceeds a cap on either axis (so a window is
        needed). Independent of the surface toggle (viewport_mode) — the
        command layer ANDs this with whether the surface opts in."""
        cw = max(1, int(self.rules.get("viewport_width", 30)))
        ch = max(1, int(self.rules.get("viewport_height", 30)))
        return self.grid_width > cw or self.grid_height > ch

    def _clamp_view(self, x: int, y: int,
                    vw: int, vh: int) -> Tuple[int, int]:
        """Clamp a top-left anchor so the vw x vh window stays on the grid."""
        x = max(1, min(int(x), self.grid_width - vw + 1))
        y = max(1, min(int(y), self.grid_height - vh + 1))
        return x, y

    def resolve_viewport(self, channel_key: str, *,
                         enabled: bool) -> Optional[Tuple[int, int, int, int]]:
        """The (vx, vy, vw, vh) window to render for `channel_key`, or None
        for the whole grid. None when the viewport is disabled for this
        surface (enabled=False) or the grid fits both caps. The offset comes
        from channel_views (default top-left), clamped to the grid."""
        if not enabled or not self.viewport_engaged():
            return None
        vw, vh = self._viewport_dims()
        off = self.channel_views.get(channel_key)
        ox, oy = (off[0], off[1]) if isinstance(off, (list, tuple)) and \
            len(off) == 2 else (1, 1)
        vx, vy = self._clamp_view(ox, oy, vw, vh)
        return (vx, vy, vw, vh)

    def set_view(self, channel_key: str, x: int, y: int) -> Tuple[int, int]:
        """Set a channel's viewport top-left to (x, y) (clamped). Returns the
        stored (clamped) anchor."""
        vw, vh = self._viewport_dims()
        vx, vy = self._clamp_view(x, y, vw, vh)
        self.channel_views[channel_key] = [vx, vy]
        return vx, vy

    def center_view(self, channel_key: str,
                    cx: int, cy: int) -> Tuple[int, int]:
        """Center a channel's viewport on (cx, cy) as nearly as the grid
        allows (the "move the camera to the protagonist" helper). Returns the
        stored (clamped) top-left anchor."""
        vw, vh = self._viewport_dims()
        return self.set_view(channel_key, cx - vw // 2, cy - vh // 2)

    def pan_view(self, channel_key: str, dx: int, dy: int) -> Tuple[int, int]:
        """Shift a channel's viewport by (dx, dy) tiles from its current
        (clamped) anchor. Returns the new (clamped) anchor."""
        vw, vh = self._viewport_dims()
        off = self.channel_views.get(channel_key)
        ox, oy = (off[0], off[1]) if isinstance(off, (list, tuple)) and \
            len(off) == 2 else (1, 1)
        ox, oy = self._clamp_view(ox, oy, vw, vh)
        return self.set_view(channel_key, ox + int(dx), oy + int(dy))

    def clear_view(self, channel_key: str) -> None:
        """Forget a channel's viewport offset (snaps back to top-left)."""
        self.channel_views.pop(channel_key, None)

    def render_ascii(self, pov_team: Optional[str] = None,
                     colorize: bool = False,
                     hidden_layers: Optional["set[str]"] = None,
                     viewport: Optional[Tuple[int, int, int, int]] = None,
                     legend: bool = False) -> str:
        """Render the ASCII map. Thin wrapper that activates the read-only
        _fog_team_sees memo for the duration of the render, so the per-cell,
        per-layer fog scan doesn't recompute the same team-sight (each
        recompute loops every team member with a per-member LOS walk). The
        memo is torn down in `finally` and is never held across a mutation, so
        it can't go stale. See _render_ascii_impl for the actual rendering.

        `hidden_layers` (None = use this match's persistent self.hidden_layers)
        suppresses render layers by name: zones / tiles / entities / fog."""
        hidden = self.hidden_layers if hidden_layers is None else hidden_layers
        prev = self._vision_memo
        if prev is None:
            self._vision_memo = {}
        try:
            return self._render_ascii_impl(pov_team, colorize, hidden,
                                           viewport=viewport, legend=legend)
        finally:
            self._vision_memo = prev

    def _render_ascii_impl(self, pov_team: Optional[str] = None,
                           colorize: bool = False,
                           hidden: "set[str]" = frozenset(),
                           viewport: Optional[Tuple[int, int, int, int]] = None,
                           legend: bool = False) -> str:
        # `pov_team` filters EVERY layer through its visibility rule: a
        # zone / tile / entity hidden from that team isn't painted (its
        # cell falls back to whatever layer is visible underneath, so the
        # map doesn't betray a hidden feature's position). pov_team=None
        # = omniscient (no filtering).
        # Build grid with an unused 0th row/col so coordinates can be 1-based.
        # `colors` is a parallel grid of palette NAMES (or None) — each
        # layer that owns a cell sets BOTH its glyph and its color (color
        # None = uncolored), so the topmost feature owns the cell's tint
        # (keeping the existing entity > tile > zone layering). A zone/tile
        # with a color but no glyph still tints its cell (the '.'), the
        # "fill empty cells" behavior.
        grid = [
            ["." for _ in range(self.grid_width + 1)]
            for _ in range(self.grid_height + 1)
        ]
        colors: List[List[Optional[str]]] = [
            [None for _ in range(self.grid_width + 1)]
            for _ in range(self.grid_height + 1)
        ]
        # `meanings` (only allocated for the auto-legend) is a parallel grid
        # of "what is this glyph" strings, set alongside each painted glyph
        # so the legend reflects the FINAL top-layer glyph actually shown
        # (and only the cells in the rendered window). None = nothing to
        # explain (the background '.').
        meanings: Optional[List[List[Optional[str]]]] = None
        if legend:
            meanings = [
                [None for _ in range(self.grid_width + 1)]
                for _ in range(self.grid_height + 1)
            ]

        # Zone layer (lowest): a single-char `glyph` paints all the zone's
        # cells; a `color` tints them (the glyph if present, else the '.').
        # Overlapping zones: later-iterated wins for both.
        for zname, z in (self.zones.items() if "zones" not in hidden else ()):
            if not self.zone_visible_to(zname, pov_team):
                continue
            g = z.get("glyph")
            has_glyph = isinstance(g, str) and len(g) == 1
            zc = self.zone_color(zname)
            if not has_glyph and zc is None:
                continue
            for (zx, zy) in z.get("cells", ()):
                if not self.in_bounds(zx, zy):
                    continue
                if has_glyph:
                    grid[zy][zx] = g
                    colors[zy][zx] = zc
                    if meanings is not None:
                        meanings[zy][zx] = f"zone: {zname}"
                elif zc is not None:
                    colors[zy][zx] = zc

        # Tile layer: instance `glyph` wins, else the template's. A tile
        # `color` (instance > template) tints the cell — overriding any zone
        # tint underneath, since the tile is the higher layer.
        for (tx, ty), data in (self.tiles.items() if "tiles" not in hidden else ()):
            if not self.in_bounds(tx, ty):
                continue
            if not self.tile_visible_to(tx, ty, pov_team):
                continue
            glyph = data.get("glyph")
            if not (isinstance(glyph, str) and len(glyph) == 1):
                tpl_name = data.get("_template")
                if isinstance(tpl_name, str):
                    tpl = self.tile_templates.get(tpl_name)
                    if tpl is not None:
                        cand = tpl.data.get("glyph")
                        if isinstance(cand, str) and len(cand) == 1:
                            glyph = cand
            has_glyph = isinstance(glyph, str) and len(glyph) == 1
            tc = self.tile_color(tx, ty)
            if has_glyph:
                grid[ty][tx] = glyph
                colors[ty][tx] = tc            # tile owns the cell (tc may be None)
                if meanings is not None:
                    tname = data.get("_template")
                    meanings[ty][tx] = (f"tile: {tname}"
                                        if isinstance(tname, str) and tname
                                        else "tile")
            elif tc is not None:
                colors[ty][tx] = tc

        # Entity layer (top, above tiles): the "who is where" question wins
        # over tile features. An entity always paints a glyph, so it owns
        # the cell's color too (its resolved color, or None = uncolored,
        # overriding any tile/zone tint).
        for e in (self.entities.values() if "entities" not in hidden else ()):
            if not getattr(e, "is_alive", True):
                continue
            # Glued body parts ride on the parent's cell — the parent draws
            # the glyph; they never paint their own. A LOCATED part draws at
            # its own cell. REGION parts overlap the parent and are deferred
            # to a priority pass below. Mounted riders are aboard: hidden
            # ones don't draw (the vehicle does); visible (region-slot) ones
            # draw over the vehicle in the priority pass below.
            if e.is_glued_part or e.is_region_part or e.is_mounted:
                continue
            if not self.entity_visible_to(e.id, pov_team):
                continue
            sym = self.entity_glyph(e, pov_team)
            ecol = self.entity_color(e, pov_team)
            for (cx, cy) in self.entity_cells(e):
                if self.in_bounds(cx, cy):
                    grid[cy][cx] = sym
                    colors[cy][cx] = ecol
                    if meanings is not None:
                        meanings[cy][cx] = self.entity_display_name(e, pov_team)

        # Visible-rider priority pass: a rider in a region slot sits ON the
        # vehicle, so it draws over the vehicle glyph at its slot cell.
        for e in (self.entities.values() if "entities" not in hidden else ()):
            if not e.is_visible_rider or not getattr(e, "is_alive", True):
                continue
            if not self.entity_visible_to(e.id, pov_team):
                continue
            sym = self.entity_glyph(e, pov_team)
            ecol = self.entity_color(e, pov_team)
            for (cx, cy) in self.entity_cells(e):
                if self.in_bounds(cx, cy):
                    grid[cy][cx] = sym
                    colors[cy][cx] = ecol
                    if meanings is not None:
                        meanings[cy][cx] = self.entity_display_name(e, pov_team)

        # Region-part priority pass: a region part overlaps its parent, so by
        # default it only draws over the parent when it has a CUSTOM glyph (a
        # default-glyph region part yields, so it doesn't clobber the parent's
        # own customization). part_custom_glyph_priority=False = parent always
        # wins. Drawn regardless of hp so structural 0/0 zones still show.
        if bool(self.rules.get("part_custom_glyph_priority", True)) and "entities" not in hidden:
            for e in self.entities.values():
                if not e.is_region_part or not self.entity_has_custom_glyph(e):
                    continue
                if not self.entity_visible_to(e.id, pov_team):
                    continue
                sym = self.entity_glyph(e, pov_team)
                ecol = self.entity_color(e, pov_team)
                for (cx, cy) in self.entity_cells(e):
                    if self.in_bounds(cx, cy):
                        grid[cy][cx] = sym
                        colors[cy][cx] = ecol
                        if meanings is not None:
                            meanings[cy][cx] = self.entity_display_name(e, pov_team)

        # Fog overlay (last): an unrevealed cell shows fog_glyph and no
        # color (fog hides whatever tint was there).
        if self.fog_enabled and pov_team is not None and "fog" not in hidden:
            fog_glyph = str(self.rules.get("fog_glyph", "?"))
            fg = fog_glyph[0] if fog_glyph else "."
            for yy in range(1, self.grid_height + 1):
                for xx in range(1, self.grid_width + 1):
                    if not self._fog_terrain_visible(pov_team, xx, yy):
                        grid[yy][xx] = fg
                        colors[yy][xx] = None
                        if meanings is not None:
                            meanings[yy][xx] = ("fog (unseen)"
                                                if fg != "." else None)

        # Window bounds: the viewport (vx, vy, vw, vh) clips the rendered
        # cells to a sub-rectangle of the grid (clamped); None = whole grid.
        if viewport is not None:
            vx, vy, vw, vh = viewport
            x0 = max(1, vx)
            y0 = max(1, vy)
            x1 = min(self.grid_width, vx + vw - 1)
            y1 = min(self.grid_height, vy + vh - 1)
        else:
            x0, y0, x1, y1 = 1, 1, self.grid_width, self.grid_height

        # Compose, wrapping each cell in its ANSI color when colorizing.
        lines = []
        for yy in range(y0, y1 + 1):
            cells = []
            for xx in range(x0, x1 + 1):
                ch = grid[yy][xx]
                if colorize:
                    name = colors[yy][xx]
                    code = TEXT_COLORS.get(name) if name else None
                    if code:
                        ch = f"\x1b[{code}m{ch}\x1b[0m"
                cells.append(ch)
            lines.append(" ".join(cells))
        out = "\n".join(lines)

        # Auto-legend: glyph -> meanings, collected from the cells actually
        # rendered (within the window) so it explains only what's on screen.
        if meanings is not None:
            legend_map: Dict[str, List[str]] = {}
            for yy in range(y0, y1 + 1):
                for xx in range(x0, x1 + 1):
                    mean = meanings[yy][xx]
                    if not mean:
                        continue
                    glyph = grid[yy][xx]
                    bucket = legend_map.setdefault(glyph, [])
                    if mean not in bucket:
                        bucket.append(mean)
            if legend_map:
                leg_lines = ["Legend:"]
                for glyph in sorted(legend_map):
                    leg_lines.append(
                        f"  {glyph} — " + ", ".join(legend_map[glyph]))
                out = out + "\n" + "\n".join(leg_lines)
        return out

    # ---- graphics render model (render_scene) -----------------------
    # The sprite analog of render_ascii: a DECLARATIVE scene the graphics
    # surface (gui.py) draws. The engine never touches pixels — it emits
    # sprite KEYS + tint/opacity/flip/mode + fog + borders + background.
    # KEEP THE LAYER ORDER / VISIBILITY / POV / FOG LOGIC IN SYNC WITH
    # _render_ascii_impl (a parallel method, by design, to avoid risking the
    # heavily-used ASCII path; both reuse the same predicate + resolver
    # helpers, so only the loop skeleton is duplicated).
    def render_scene(self, pov_team: Optional[str] = None,
                     hidden_layers: Optional["set[str]"] = None,
                     viewport: Optional[Tuple[int, int, int, int]] = None
                     ) -> Dict[str, Any]:
        """Build the graphics render model (see _render_scene_impl). Activates
        the read-only fog-sight memo for the duration, exactly like
        render_ascii."""
        hidden = self.hidden_layers if hidden_layers is None else hidden_layers
        prev = self._vision_memo
        if prev is None:
            self._vision_memo = {}
        try:
            return self._render_scene_impl(pov_team, hidden, viewport)
        finally:
            self._vision_memo = prev

    @staticmethod
    def _coerce_layer(raw: Any, default: int) -> int:
        """A sprite Z-layer value: a number wins, anything else falls back to
        `default` (so a junk override never breaks the draw order)."""
        if isinstance(raw, bool) or raw is None:
            return default
        if isinstance(raw, (int, float)):
            return int(raw)
        if isinstance(raw, str):
            try:
                return int(float(raw.strip()))
            except (ValueError, AttributeError):
                return default
        return default

    def _layer_rule(self, key: str, default: int) -> int:
        return self._coerce_layer(self.rules.get(key, default), default)

    def _emit_entity_placement(self, out: List[Dict[str, Any]], e: "Entity",
                               pov_team: Optional[str], layer: int) -> None:
        w, h = self.entity_footprint(e)
        spr = self.entity_sprite(e, pov_team)
        key, fh, fv = spr if spr is not None else (None, False, False)
        mode = self.entity_sprite_mode(e) if (w > 1 or h > 1) else "single"
        # Per-entity override: a `sprite_layer` var wins over the pass default.
        eff_layer = self._coerce_layer(e.vars.get("sprite_layer"), layer)
        out.append({
            "kind": "entity", "ref": e.id,
            "x": e.x, "y": e.y, "w": w, "h": h, "mode": mode,
            "sprite": key, "glyph": self.entity_glyph(e, pov_team),
            "tint": self.entity_color(e, pov_team), "opacity": 100,
            "flip_h": fh, "flip_v": fv, "layer": eff_layer,
        })
        # Overlay sprites (status FX + overlay var), drawn over the entity at
        # their own layer (default 150). Footprint + mode follow the entity so
        # a multi-tile body's overlay covers the whole body. Graphics-only.
        for ov in self.entity_overlays(e, pov_team):
            out.append({
                "kind": "overlay", "ref": e.id,
                "x": e.x, "y": e.y, "w": w, "h": h, "mode": mode,
                "sprite": ov["sprite"], "glyph": None,
                "tint": ov["tint"], "opacity": ov["opacity"],
                "flip_h": False, "flip_v": False, "layer": ov["layer"],
            })

    def _scene_borders(self) -> Dict[str, Any]:
        """Border (grid-line) config for the scene: the show_borders /
        border_color / border_opacity rules, with per-MATCH overrides
        (border_show / border_color / border_opacity fields) winning over the
        rule, plus per-TILE `border_color` / `border_opacity` data overrides
        (keyed 'x,y')."""
        # Per-match override (field) > rule.
        if self.border_show is not None:
            show = bool(self.border_show)
        else:
            show = bool(self.rules.get("show_borders", True))
        if isinstance(self.border_color, str) and self.border_color:
            color = self.border_color
        else:
            color = str(self.rules.get("border_color", "white"))
        raw_op = self.border_opacity if self.border_opacity is not None \
            else self.rules.get("border_opacity", 100)
        try:
            op = int(raw_op)
        except (TypeError, ValueError):
            op = 100
        overrides: Dict[str, Any] = {}
        for (tx, ty), data in self.tiles.items():
            if not isinstance(data, dict):
                continue
            bc, bo = data.get("border_color"), data.get("border_opacity")
            ov: Dict[str, Any] = {}
            if isinstance(bc, str) and bc:
                ov["color"] = bc
            if isinstance(bo, (int, float)) and not isinstance(bo, bool):
                ov["opacity"] = max(0, min(100, int(bo)))
            if ov:
                overrides[f"{tx},{ty}"] = ov
        return {"show": show, "color": color,
                "opacity": max(0, min(100, op)), "overrides": overrides}

    def _render_scene_impl(self, pov_team: Optional[str],
                           hidden: "set[str]",
                           viewport: Optional[Tuple[int, int, int, int]]
                           ) -> Dict[str, Any]:
        placements: List[Dict[str, Any]] = []
        # Z-layer defaults per kind (configurable via the sprite_layer_* rules;
        # per-item overrides handled at each emission). Background is the floor.
        zone_layer = self._layer_rule("sprite_layer_zone", 25)
        tile_layer = self._layer_rule("sprite_layer_tile", 50)
        corpse_layer = self._layer_rule("sprite_layer_corpse", 75)
        entity_layer = self._layer_rule("sprite_layer_entity", 100)
        rider_layer = self._layer_rule("sprite_layer_rider", 110)

        # Zone layer: a sprite / glyph / color per covered cell.
        for zname, z in (self.zones.items() if "zones" not in hidden else ()):
            if not self.zone_visible_to(zname, pov_team):
                continue
            spr = self.zone_sprite(zname)
            g = z.get("glyph")
            g = g if isinstance(g, str) and len(g) == 1 else None
            tint = self.zone_color(zname)
            if spr is None and g is None and tint is None:
                continue
            for (zx, zy) in z.get("cells", ()):
                if not self.in_bounds(zx, zy):
                    continue
                placements.append({
                    "kind": "zone", "ref": zname, "x": zx, "y": zy,
                    "w": 1, "h": 1, "mode": "single", "sprite": spr,
                    "glyph": g, "tint": tint, "opacity": 100,
                    "flip_h": False, "flip_v": False, "layer": zone_layer})

        # Tile layer (20).
        for (tx, ty), data in (self.tiles.items() if "tiles" not in hidden else ()):
            if not self.in_bounds(tx, ty) or not self.tile_visible_to(tx, ty, pov_team):
                continue
            spr = self.tile_sprite(tx, ty)
            g = self.tile_glyph(tx, ty)
            tint = self.tile_color(tx, ty)
            if spr is None and g is None and tint is None:
                continue
            # Per-tile override: a `sprite_layer` data field wins over the rule.
            t_layer = self._coerce_layer(
                data.get("sprite_layer") if isinstance(data, dict) else None,
                tile_layer)
            placements.append({
                "kind": "tile", "ref": f"{tx},{ty}", "x": tx, "y": ty,
                "w": 1, "h": 1, "mode": "single", "sprite": spr,
                "glyph": g, "tint": tint, "opacity": 100,
                "flip_h": False, "flip_v": False, "layer": t_layer})

        # Corpse layer (25): the dead entity's stored sprite, tinted +
        # semi-transparent (corpse_sprite_tint / corpse_sprite_opacity).
        # Fog-gated like everything else; only drawn when it has a sprite (or
        # a stored glyph for the text fallback). Rides the entity-layer toggle.
        if "entities" not in hidden:
            c_tint = str(self.rules.get("corpse_sprite_tint", "gray")).strip() or None
            try:
                c_op = int(self.rules.get("corpse_sprite_opacity", 50))
            except (TypeError, ValueError):
                c_op = 50
            c_op = max(0, min(100, c_op))
            for (cx, cy, cid, corpse) in self.all_corpses():
                if not self.corpse_visible_to(cid, corpse, cx, cy, pov_team):
                    continue
                spr = self.corpse_sprite(corpse)
                gl = ((corpse.get("entity") or {}).get("vars") or {}).get("glyph")
                gl = gl if isinstance(gl, str) and len(gl) == 1 else None
                if spr is None and gl is None:
                    continue
                cw, ch = self._corpse_footprint(corpse)
                placements.append({
                    "kind": "corpse", "ref": cid, "x": cx, "y": cy,
                    "w": cw, "h": ch, "mode": "single", "sprite": spr,
                    "glyph": gl, "tint": c_tint, "opacity": c_op,
                    "flip_h": False, "flip_v": False, "layer": corpse_layer})

        if "entities" not in hidden:
            # Entity main pass (30): not glued/region/mounted.
            for e in self.entities.values():
                if not getattr(e, "is_alive", True):
                    continue
                if e.is_glued_part or e.is_region_part or e.is_mounted:
                    continue
                if not self.entity_visible_to(e.id, pov_team):
                    continue
                self._emit_entity_placement(placements, e, pov_team, entity_layer)
            # Visible riders, over their host.
            for e in self.entities.values():
                if not e.is_visible_rider or not getattr(e, "is_alive", True):
                    continue
                if not self.entity_visible_to(e.id, pov_team):
                    continue
                self._emit_entity_placement(placements, e, pov_team, rider_layer)
            # Region parts with a custom sprite/glyph, over their parent.
            if bool(self.rules.get("part_custom_glyph_priority", True)):
                for e in self.entities.values():
                    if not e.is_region_part:
                        continue
                    if not (self.entity_has_custom_sprite(e)
                            or self.entity_has_custom_glyph(e)):
                        continue
                    if not self.entity_visible_to(e.id, pov_team):
                        continue
                    self._emit_entity_placement(placements, e, pov_team, rider_layer)

        # Fog: cells the POV team can't see (always drawn last / on top).
        fog: List[Dict[str, Any]] = []
        if self.fog_enabled and pov_team is not None and "fog" not in hidden:
            fog_sprite = str(self.rules.get("fog_sprite", "")).strip() or None
            try:
                fog_op = int(self.rules.get("fog_opacity", 60))
            except (TypeError, ValueError):
                fog_op = 60
            fog_op = max(0, min(100, fog_op))
            for yy in range(1, self.grid_height + 1):
                for xx in range(1, self.grid_width + 1):
                    if not self._fog_terrain_visible(pov_team, xx, yy):
                        fog.append({"x": xx, "y": yy,
                                    "sprite": fog_sprite, "opacity": fog_op})

        vp = None
        if viewport is not None:
            vx, vy, vw, vh = viewport
            vp = {"x": vx, "y": vy, "w": vw, "h": vh}
        return {
            "grid_width": self.grid_width, "grid_height": self.grid_height,
            "viewport": vp, "background": self.background_layer(),
            "placements": placements, "fog": fog,
            "borders": self._scene_borders(),
        }

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
        # Mounts: a member riding a vehicle has its position controlled by the
        # vehicle, not the group, so it must not be force-moved here (the
        # group path hand-rolls move_to and would otherwise desync the rider
        # off its mount). Per mount_group_move_mode: 'abort' refuses the whole
        # move; 'skip' (default) drops the riders (they stay aboard — and if
        # their vehicle is itself a group member it carries them normally).
        mounted = [eid for eid in members if self.entities[eid].is_mounted]
        if mounted:
            mode = str(self.rules.get("mount_group_move_mode", "skip"))
            if mode == "abort":
                raise VTTError(
                    f"Group '{group_name}' move aborted: member(s) "
                    f"{', '.join(repr(e) for e in mounted)} are riding a "
                    f"vehicle (dismount first, or set "
                    f"mount_group_move_mode=skip)."
                )
            skip = set(mounted)
            members = [eid for eid in members if eid not in skip]
            if not members:
                total = sum(max(1, int(n)) for _, n in moves)
                return 0, total, []
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
                    # Per-step: the WHOLE swept footprint must be in bounds and
                    # unblocked (a body can't squeeze through a gap narrower
                    # than itself, and can't cross impassable terrain) — same
                    # contract as Entity.move_dirs. Intermediate occupancy is
                    # passed through; only the FINAL footprint is occupancy-
                    # checked (below).
                    for cx, cy in self.entity_cells(e, nx, ny):
                        if not self.in_bounds(cx, cy):
                            raise OutOfBounds(
                                f"Group '{group_name}' move aborted: member "
                                f"'{eid}' would leave the grid at ({cx},{cy})."
                            )
                        if self._check_block(eid, cx, cy, "walk"):
                            raise Blocked(
                                f"Group '{group_name}' move aborted: member "
                                f"'{eid}' is blocked at ({cx},{cy})."
                            )
                    path.append((nx, ny, step_facing))
                    x, y = nx, ny
            # Final-footprint occupancy: ignore the entity itself AND every
            # other group member (they're moving too — treat as transparent),
            # plus the mover's own snake segments. cell_occupant is footprint-
            # aware and already skips glued parts / mounted riders, so a large
            # member can't land overlapping a stationary body's non-anchor
            # cells. Stackable / glued movers skip the gate (as elsewhere).
            glued = bool(e.part_of) and not e.vars.get("__part_located")
            if not e.is_cell_stackable and not glued:
                ignore = self._occupancy_ignore(e, extra=tuple(member_set))
                for cx, cy in self.entity_cells(e, x, y):
                    occ = self.cell_occupant(cx, cy, ignore)
                    if occ is not None:
                        raise Occupied(
                            f"Group '{group_name}' move aborted: member "
                            f"'{eid}' would land on ({cx},{cy}), occupied by "
                            f"'{occ}'."
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
                # Footprint-aware per-step hooks (a multi-tile member must
                # fire tile/zone hooks for EVERY covered cell it vacates /
                # enters, not just its anchor) — identical to the anchor
                # form for a 1×1 member, matching Entity.move_dirs.
                step_from_x, step_from_y = e.x, e.y
                old_cells = self.entity_cells(e, e.x, e.y)
                new_cells = self.entity_cells(e, nx, ny)
                if fire_hooks:
                    log.extend(self.fire_footprint_tile_exit(eid, old_cells, new_cells))
                    log.extend(self.fire_footprint_zone_exit(eid, old_cells, new_cells))
                e.facing = facing
                e.move_to(nx, ny)
                if fire_hooks:
                    log.extend(self.fire_footprint_tile_enter(eid, old_cells, new_cells))
                    log.extend(self.fire_footprint_zone_enter(
                        eid, old_cells, new_cells, False))
                    # Per-step entity hook, AFTER on_enter (so a passive sees
                    # the just-entered tile's effect) — full parity with
                    # single-entity move_dirs, which group move previously
                    # lacked. Drives per-cell reactions and snake-trail follow.
                    log.extend(self.fire_entity_step(
                        eid, step_from_x, step_from_y, nx, ny,
                    ))
            if fire_hooks and path:
                final_cells = self.entity_cells(e, e.x, e.y)
                log.extend(self.fire_footprint_tile_stop(eid, final_cells))
                log.extend(self.fire_footprint_zone_stop(eid, final_cells))
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
        # Seed the per-match fog toggle from the system default. After this
        # it's match state (toggled via `!match fog`), independent of the
        # rule — a refresh won't flip it.
        m.fog_enabled = bool(rules.get("fog_enabled_by_default", False))
        m.fog_memory = bool(rules.get("fog_memory_enabled_by_default", False))
        # Seed the per-match auto-legend toggle from the system default
        # (then match state, flipped via `!map legend on|off`).
        m.map_legend_enabled = bool(rules.get("map_legend_by_default", False))
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

    def copy_entity(self, src_mid: str, dest_mid: str, eid: str,
                    x: Optional[int] = None, y: Optional[int] = None,
                    *, move: bool = False) -> Tuple[str, List[str]]:
        """Copy (move=False) or MOVE (move=True) an entity — with its vars,
        statuses, passives, clamps, facing, and its whole body-part subtree —
        from one live match to another. Returns (new_id, spawn_log).

        Placement defaults to the entity's current cell; pass x/y to override.
        Parts ride along: glued/region parts are re-stamped onto the new
        anchor, a located part keeps its offset from the anchor. Ids that
        collide in the destination are auto-suffixed (`goblin` -> `goblin_2`),
        with `part_of` links remapped to match. Each spawn fires
        on_entity_spawned in the destination (the transfer routes through
        Entity.spawn, not a raw insert). The template-save-for-later flow is
        a separate feature; this is the direct match->match transfer."""
        if src_mid not in self.matches:
            raise NotFound(f"Source match '{src_mid}' not found.")
        if dest_mid not in self.matches:
            raise NotFound(f"Destination match '{dest_mid}' not found.")
        src, dest = self.matches[src_mid], self.matches[dest_mid]
        if src is dest:
            raise VTTError("Source and destination matches must differ.")
        e = src.entities.get(eid)
        if e is None:
            raise NotFound(f"Entity '{eid}' not found in match '{src_mid}'.")
        if e.is_part:
            raise VTTError(
                f"'{eid}' is a body part — transfer its parent instead.")
        # BFS the part subtree so parents are spawned before their children.
        order = [eid]
        i = 0
        while i < len(order):
            pid = order[i]; i += 1
            for child in src.entities.values():
                if child.part_of == pid and child.id not in order:
                    order.append(child.id)
        # Remap ids to avoid destination collisions (live ids + corpses).
        taken = set(dest._taken_entity_ids())
        idmap: Dict[str, str] = {}
        for oid in order:
            nid = oid
            if nid in taken:
                k = 2
                while f"{oid}_{k}" in taken:
                    k += 1
                nid = f"{oid}_{k}"
            idmap[oid] = nid
            taken.add(nid)
        tx = e.x if x is None else int(x)
        ty = e.y if y is None else int(y)
        log: List[str] = []
        for oid in order:
            oe = src.entities[oid]
            ne = Entity.from_dict(oe.to_dict())
            ne.id = idmap[oid]
            if ne.part_of:
                ne.part_of = idmap.get(ne.part_of, ne.part_of)
            # Segment chains: remap the `__follows` back-pointer (an entity id)
            # like part_of, so a copied/transferred snake keeps its chain.
            fol = ne.vars.get("__follows")
            if fol in idmap:
                ne.vars["__follows"] = idmap[fol]
            # Offset the head's snake-trail coords to the destination cell so a
            # transferred path-mode snake re-lays at its new location, not the
            # source's (same delta the anchor moves by).
            Match._shift_snake_path_vars(ne.vars, tx - e.x, ty - e.y)
            if oid == eid or not oe.is_located_part:
                px, py = tx, ty
            else:  # a located part keeps its offset from the anchor
                px, py = tx + (oe.x - e.x), ty + (oe.y - e.y)
            _, slog = ne.spawn(dest, px, py)
            log.extend(slog)
        dest._restamp_parts_for(idmap[eid])
        if move:
            for oid in reversed(order):  # children first
                if oid in src.entities:
                    src.entities[oid].remove()
        return idmap[eid], log

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