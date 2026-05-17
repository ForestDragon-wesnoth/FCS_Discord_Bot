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
    t = token.strip().lower()
    if t in ("true", "t"): return True
    if t in ("false", "f"): return False
    # If you prefer *only* true/false, delete lines above and use the two ifs.
    raise VTTError("Expected a boolean: use 'true' or 'false'.")

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
HOOK_NAMES: Set[str] = {
    "on_turn_start",
    "on_turn_end",
    "on_round_start",
    "on_round_end",
    "on_entity_spawned",
}


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
    """
    id: str
    when: str
    formula: str

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

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "when": self.when, "formula": self.formula}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Passive":
        return Passive(id=d["id"], when=d["when"], formula=d["formula"])


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
        hp_var, _, _ = self._vital_var_names()
        self.vars[hp_var] = int(value)

    @property
    def max_hp(self) -> Optional[int]:
        _, max_hp_var, _ = self._vital_var_names()
        v = self.vars.get(max_hp_var)
        return int(v) if v is not None else None

    @max_hp.setter
    def max_hp(self, value):
        _, max_hp_var, _ = self._vital_var_names()
        if value is None:
            self.vars.pop(max_hp_var, None)
        else:
            self.vars[max_hp_var] = int(value)

    @property
    def initiative(self) -> Optional[int]:
        _, _, init_var = self._vital_var_names()
        v = self.vars.get(init_var)
        return int(v) if v is not None else None

    @initiative.setter
    def initiative(self, value):
        _, _, init_var = self._vital_var_names()
        if value is None:
            self.vars.pop(init_var, None)
        else:
            self.vars[init_var] = int(value)

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
        self.hp = self.hp - max(0, amount)

    def heal(self, amount: int):
        self.hp = min(self.max_hp, self.hp + max(0, amount))

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
        # Auto-fill max_hp if missing
        max_hp_var = match.rules.get("max_hp_var", "max_hp")
        if max_hp_var not in self.vars:
            self.vars[max_hp_var] = self.vars[hp_var]

        self.move_to(x, y)

        self.bind(match, set_spawn_facing=True)
        if initiative is not None:
            self.initiative = initiative

        match.entities[self.id] = self
        match._rebuild_turn_order()
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