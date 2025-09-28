## logic.py (Core, testable)

# logic.py
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple, Set
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
    ac: Optional[int] = None
    status: Set[str] = field(default_factory=set)
    initiative: Optional[int] = None

    def __post_init__(self):
        if self.max_hp is None:
            self.max_hp = self.hp

    # ------------- runtime helpers -------------
    @property
    def is_alive(self) -> bool:
        return self.hp > 0

    def move_to(self, x: int, y: int):
        self.x, self.y = x, y

    def take_damage(self, amount: int):
        #health CAN go into the negatives, 
        self.hp = self.hp - max(0, amount)
        #TODO: add a die event if hp becomes equal or below 0 (with the possible revive support)

    def heal(self, amount: int):
        self.hp = min(self.max_hp, self.hp + max(0, amount))

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["status"] = list(self.status)
        return d

    @staticmethod
    def from_dict(data: Dict) -> "Entity":
        data = dict(data)
        data["status"] = set(data.get("status", []))
        return Entity(**data)


@dataclass
class Match:
    # explicit, user-provided id
    name: str
    #important: coordinates start from 1, not 0
    grid_width: int
    grid_height: int
    id: str
    entities: Dict[str, Entity] = field(default_factory=dict)
    turn_order: List[str] = field(default_factory=list)  # list of entity ids
    active_index: int = 0
    rules: Dict[str, str] = field(default_factory=dict)  # arbitrary key→value

    # ------------- grid helpers -------------
    def in_bounds(self, x: int, y: int) -> bool:
        """
        1-based coordinates: valid cells are
        x in [1, grid_width], y in [1, grid_height].
        """
        return 1 <= x <= self.grid_width and 1 <= y <= self.grid_height

    def is_occupied(self, x: int, y: int, ignore_entity_id: Optional[str] = None) -> bool:
        #only alive entities can occupy a space for now

        #TODO: test edge cases once I implement reviving

        for e in self.entities.values():
            if ignore_entity_id and e.id == ignore_entity_id:
                continue
            if e.x == x and e.y == y and e.is_alive:
                return True
        return False

    # ------------- entity management -------------
    def add_entity(self, e: Entity, x: int, y: int, initiative: Optional[int] = None):
        if not self.in_bounds(x, y):
            raise OutOfBounds(f"({x},{y}) outside {self.grid_width}x{self.grid_height}")
        if self.is_occupied(x, y):
            raise Occupied(f"Cell ({x},{y}) already occupied")
        if e.id in self.entities:
            raise DuplicateId(f"Entity id '{e.id}' already exists in this match")
        e.move_to(x, y)
        if initiative is not None:
            e.initiative = initiative
        self.entities[e.id] = e
        self._rebuild_turn_order()
        return e.id

    def remove_entity(self, entity_id: str):
        if entity_id not in self.entities:
            raise NotFound("Entity not found")
        del self.entities[entity_id]
        self._rebuild_turn_order()
        if self.active_index >= len(self.turn_order):
            self.active_index = 0

    def move_entity(self, entity_id: str, x: int, y: int):
        e = self.entities.get(entity_id)
        if not e:
            raise NotFound("Entity not found")
        if not self.in_bounds(x, y):
            raise OutOfBounds(f"({x},{y}) outside {self.grid_width}x{self.grid_height}")
        if self.is_occupied(x, y, ignore_entity_id=entity_id):
            raise Occupied(f"Cell ({x},{y}) already occupied")
        e.move_to(x, y)

    def damage(self, entity_id: str, amount: int):
        e = self.entities.get(entity_id)
        if not e:
            raise NotFound("Entity not found")
        e.take_damage(amount)

    def heal(self, entity_id: str, amount: int):
        e = self.entities.get(entity_id)
        if not e:
            raise NotFound("Entity not found")
        e.heal(amount)

    def set_initiative(self, entity_id: str, init_value: int):
        e = self.entities.get(entity_id)
        if not e:
            raise NotFound("Entity not found")
        e.initiative = init_value
        self._rebuild_turn_order()

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

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "grid_width": self.grid_width,
            "grid_height": self.grid_height,
            "id": self.id,
            "entities": {eid: e.to_dict() for eid, e in self.entities.items()},
            "turn_order": list(self.turn_order),
            "active_index": self.active_index,
            "rules": dict(self.rules),
        }

    @staticmethod
    def from_dict(data: Dict) -> "Match":
        m = Match(
            name=data["name"],
            grid_width=data["grid_width"],
            grid_height=data["grid_height"],
            id=data["id"],
        )
        m.entities = {eid: Entity.from_dict(ed) for eid, ed in data.get("entities", {}).items()}
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
        for e in self.entities.values():
            if not e.is_alive:
                continue
            if self.in_bounds(e.x, e.y):
                grid[e.y][e.x] = "@"  # you can customize symbols per team

        # Skip the 0th row entirely
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
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def load(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.matches = {mid: Match.from_dict(md) for mid, md in data.get("matches", {}).items()}
        self.active_by_channel = data.get("active_by_channel", {})

#unused, now cli.py is used for local testing instead
## For quick local testing
#if __name__ == "__main__":
#    mgr = MatchManager()
#    mid = mgr.create_match("test", "Test Skirmish", 8, 6)
#    m = mgr.get(mid)
#    e1 = Entity(id="rogue", name="Rogue", hp=12, x=0, y=0)
#    e2 = Entity(id="goblin1", name="Goblin", hp=7, x=1, y=0)
#    m.add_entity(e1, 0, 0, initiative=17)
#    m.add_entity(e2, 1, 0, initiative=12)
#    print("Match:", m.name, m.id)
#    print(m.render_ascii())
#    print("Turn order:", m.turn_order)
#    print("Current:", m.current_entity_id())
#    m.next_turn(); print("After next:", m.current_entity_id())