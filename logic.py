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

# -------------------------
# Data Models
# -------------------------

Direction = Literal["up", "down", "left", "right"]
ALLOWED_DIRECTIONS: tuple[Direction, ...] = ("up", "down", "left", "right")

def _dominant_axis_dir(dx: int, dy: int) -> Direction:
    # ties prefer vertical (feel free to change)
    if abs(dy) >= abs(dx):
        return "down" if dy > 0 else "up"
    else:
        return "right" if dx > 0 else "left"

def _default_facing_for(x: int, y: int, width: int, height: int) -> Direction:
    # Face toward the map's center (1-based grid). Center can be fractional; use dominant axis.
    cx = (width + 1) / 2
    cy = (height + 1) / 2
    dx = cx - x
    dy = cy - y
    return _dominant_axis_dir(int(round(dx)), int(round(dy)))

@dataclass
class Entity:
    # Required (no defaults) must come first in a dataclass
    name: str
    hp: int
    x: int
    y: int
    id: str  # explicit, user-provided
    # Optional / defaulted fields
    max_hp: Optional[int] = None
    team: Optional[str] = None
    status: Set[str] = field(default_factory=set)
    initiative: Optional[int] = None
    extras: Dict[str, Any] = field(default_factory=dict)  # arbitrary stats/resources
    facing: Direction = "up"  # will be set to a default by Match when adding

    #connect Entity to the Match (so functions like moving an entity can still access map size data)

    #back-reference (not serialized)
    _match: "Match | None" = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.max_hp is None:
            self.max_hp = self.hp
        if self.facing not in ALLOWED_DIRECTIONS:
            self.facing = "up"

    # ---------- binding ----------
    def bind(self, match: "Match"):
        self._match = match
        if match.in_bounds(self.x, self.y):
            try:
                self.facing = _default_facing_for(self.x, self.y, match.grid_width, match.grid_height)
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
    def spawn(self, match: "Match", x: int, y: int, initiative: Optional[int] = None):
        """
        Add this entity to a match at (x,y).
        Validates bounds/occupancy, sets facing, registers in turn order.
        """
        if self._match is not None:
            raise VTTError(f"Entity '{self.id}' is already in a match")
        if not match.in_bounds(x, y):
            raise OutOfBounds(f"({x},{y}) outside {match.grid_width}x{match.grid_height}")
        if match.is_occupied(x, y):
            raise Occupied(f"Cell ({x},{y}) already occupied")

        self.move_to(x, y)
        self.bind(match)
        if initiative is not None:
            self.initiative = initiative

        if self.id in match.entities:
            raise DuplicateId(f"Entity id '{self.id}' already exists in this match")
        match.entities[self.id] = self
        match._rebuild_turn_order()
        return self.id

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

    # ---------- serialization ----------
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("_match", None)  # do not serialize backref
        d["status"] = list(self.status)
        return d

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Entity":
        d = dict(data)
        d["status"] = set(d.get("status", []))
        d.pop("_match", None)
        return Entity(**d)

@dataclass
class Match:
    id: str
    name: str
    grid_width: int
    grid_height: int
    entities: Dict[str, Entity] = field(default_factory=dict)
    turn_order: List[str] = field(default_factory=list)
    active_index: int = 0
    rules: Dict[str, Any] = field(default_factory=dict)

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
        # Sort: higher initiative first; stable by name then id for determinism
        ordered = sorted(
            [e for e in self.entities.values() if e.initiative is not None and e.is_alive],
            key=lambda e: (-e.initiative, e.name.lower(), e.id)
        )
        self.turn_order = [e.id for e in ordered]
        # Clamp active index
        if self.turn_order:
            self.active_index %= len(self.turn_order)
        else:
            self.active_index = 0

    def current_entity_id(self) -> Optional[str]:
        if not self.turn_order:
            return None
        return self.turn_order[self.active_index]

    def next_turn(self) -> Optional[str]:
        if not self.turn_order:
            return None
        self.active_index = (self.active_index + 1) % len(self.turn_order)
        return self.current_entity_id()

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
            "rules": self.rules,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Match":
        m = Match(
            id=data["id"],
            name=data["name"],
            grid_width=data["grid_width"],
            grid_height=data["grid_height"],
        )
        # Recreate entities & bind to match; insert directly (trusted from save)
        for eid, ed in data.get("entities", {}).items():
            e = Entity.from_dict(ed)
            e.bind(m)
            m.entities[eid] = e
        m.turn_order = data.get("turn_order", [])
        m.active_index = data.get("active_index", 0)
        m.rules = data.get("rules", {})
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
# Manager (multi-match + simple persistence)
# -------------------------
class MatchManager:
    def __init__(self):
        self.matches: Dict[str, Match] = {}
        # optional: track per-channel active match
        self.active_by_channel: Dict[str, str] = {}

    def create_match(self, match_id: str, name: str, width: int, height: int) -> str:
        if match_id in self.matches:
            raise DuplicateId(f"Match id '{match_id}' already exists")
        m = Match(name=name, grid_width=width, grid_height=height, id=match_id)
        self.matches[m.id] = m
        return m.id

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
            "active_by_channel": dict(self.active_by_channel),
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
        except Exception as e:
            # Defensive: any schema mismatch should be surfaced as a friendly VTTError
            raise VTTError(f"Invalid save file format in '{path}': {e}")
    