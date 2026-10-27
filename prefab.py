"""Prefabs (#31): a saved rectangle of one map, stamped into a match.

The agreed design is docs/zones_prefabs_design.md §5-§9. A prefab holds:

  {"kind": "prefab", "width": W, "height": H, "system": <system it was
   saved under>, "definitions_all": bool, "var_names": {rule: var},
   "tiles": [[dx, dy, data], ...],          tile data without corpses
   "corpses": [[dx, dy, id, corpse], ...],  corpse snapshots, cells relative
   "zones": {name: zone dict},              cells relative, an aura's
                                            `anchor` names a saved unit
   "units": [entity dict, ...],             roots, parts and riders, each at
                                            its offset (x, y), links by
                                            their saved ids, vars in the
                                            partial-template FILE form
   "groups": {group: [saved id, ...]},
   "definitions": {library: {name: entry}}, the match-level definitions the
                                            contents use (§8)
   "saved_by": ..., "saved_by_name": ...,
   "tags": [tag, ...]}                      library tags (`!prefab tags`),
                                            for `!prefab list tag=`

Offsets are 0-based from the rectangle's top-left cell. Saved ids are link
keys: a stamp keeps a saved id while the match has it free and mints a new
one from it otherwise. The units are
stored the way the cross-match copy moves a body (MatchManager.copy_entity):
entity dicts at offsets, spawned parents first, so rotation, riders and id
remapping act on one shape.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import logic
from logic import (VTTError, Entity, Match, Map, check_id,
                   normalize_zone_def, rotate_direction)

PREFAB_KINDS: Tuple[str, ...] = ("units", "tiles", "zones", "corpses")
STAMP_MODES: Tuple[str, ...] = ("overlay", "keep", "replace")
# The libraries a prefab carries definitions from (§8).
PREFAB_LIBRARIES: Tuple[str, ...] = ("tile_templates", "status_definitions",
                                     "zone_definitions", "formula_functions")
# The var-name rules a stamp reads saved units with (a unit dict's own
# var_names first).
_FOOTPRINT_RULES = ("footprint_width_var", "footprint_height_var")
_PREFAB_KEYS = ("kind", "width", "height", "system", "definitions_all",
                "var_names", "tiles", "corpses", "zones", "units", "groups",
                "definitions", "saved_by", "saved_by_name", "tags")


def check_prefab_name(name: Any) -> str:
    """A prefab name, lower-cased (it names a file)."""
    n = str(name).strip().lower()
    check_id("prefab name", n)
    return n


# ---- capture ----------------------------------------------------------------

def _body_tree(m: Match, top: Entity) -> List[Entity]:
    """`top` and everything that travels with it: its part subtree and the
    units mounted on any of them, recursively; parents and vehicles
    before what hangs on them."""
    out: List[Entity] = []
    seen: Set[str] = set()
    queue = [top]
    while queue:
        u = queue.pop(0)
        if u.id in seen:
            continue
        seen.add(u.id)
        out.append(u)
        for p in m.entity_part_subtree(u.id):
            if p.id not in seen:
                seen.add(p.id)
                out.append(p)
        for holder in [u] + [p for p in m.entity_part_subtree(u.id)]:
            for r in m.vehicle_riders(holder.id):
                if r.id not in seen:
                    queue.append(r)
    return out


def _unit_cells(m: Match, u: Entity) -> List[Tuple[int, int]]:
    try:
        return list(m.entity_cells(u))
    except Exception:  # noqa: BLE001 - a malformed body reads as its anchor
        return [(u.x, u.y)]


def _used_funcs(m: Match, texts: List[str]) -> Set[str]:
    """The match's `!func`s named as calls in `texts`, and those they call
    in turn."""
    found: Set[str] = set()
    pending = list(texts)
    while pending:
        text = pending.pop()
        for fname, fn in m.formula_functions.items():
            if fname not in found and re.search(
                    rf"(?<![\w.]){re.escape(fname)}\s*\(", text):
                found.add(fname)
                pending.append(fn.body)
    return found


def _strings(value: Any, out: List[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _strings(v, out)
    elif isinstance(value, list):
        for v in value:
            _strings(v, out)


def capture_prefab(mgr: Any, m: Match, mp: Map, x1: int, y1: int, x2: int,
                   y2: int, only: Optional[Set[str]] = None,
                   definitions_all: bool = False
                   ) -> Tuple[Dict[str, Any], List[str]]:
    """The rectangle (x1, y1)-(x2, y2) of map `mp` as a prefab, with notes
    (⚠️ lines: bodies left out, definitions). `only` picks the kinds kept
    (PREFAB_KINDS); `definitions_all` also stores the system-level
    definitions the contents use (§8)."""
    xa, xb = sorted((int(x1), int(x2)))
    ya, yb = sorted((int(y1), int(y2)))
    for cx, cy in ((xa, ya), (xb, yb)):
        if not mp.in_bounds(cx, cy):
            raise VTTError(f"({cx},{cy}) is off the {mp.size_text()} map "
                           f"`{mp.name}`.")
    kinds = set(PREFAB_KINDS) if not only else set(only)
    notes: List[str] = []

    def inside(x: int, y: int) -> bool:
        return xa <= x <= xb and ya <= y <= yb

    data: Dict[str, Any] = {
        "kind": "prefab", "width": xb - xa + 1, "height": yb - ya + 1,
        "system": m.system_name, "definitions_all": bool(definitions_all),
        "var_names": {r: str(m.rules.get(r)) for r in
                      ("footprint_width_var", "footprint_height_var",
                       "team_var")},
        "tiles": [], "corpses": [], "zones": {}, "units": [], "groups": {},
        "definitions": {},
    }
    saved_ids: Set[str] = set()
    if "units" in kinds:
        tops = [e for e in m.entities.values()
                if e.map == mp.name and not e.part_of and not e.mounted_on]
        for top in sorted(tops, key=lambda e: (e.y, e.x, e.id)):
            tree = _body_tree(m, top)
            cells = [c for u in tree for c in _unit_cells(m, u)]
            ins = [inside(*c) for c in cells]
            if not any(ins):
                continue
            if not all(ins):
                notes.append(f"⚠️ `{top.id}` was left out: its body (parts "
                             f"and riders included) reaches past the "
                             f"rectangle; widen it to take the unit.")
                continue
            carriers = [u.id for u in tree if m.maps_carried_by(u.id)]
            if carriers:
                notes.append(f"⚠️ `{top.id}` was left out: "
                             + ", ".join(f"`{c}`" for c in carriers)
                             + " carries a map, and units carrying maps "
                               "can't go into a prefab (not supported).")
                continue
            tok = logic._SAVING_TO_LIBRARY.set(True)
            try:
                for u in tree:
                    d = u.to_dict()
                    d.pop("map", None)
                    d["x"] = int(d.get("x", u.x)) - xa
                    d["y"] = int(d.get("y", u.y)) - ya
                    dv = d.get("vars")
                    if isinstance(dv, dict):
                        Match._shift_snake_path_vars(dv, -xa, -ya)
                        d["vars"] = m.template_file_form(dv, notes,
                                                         unit_vars=True)
                    data["units"].append(d)
                    saved_ids.add(u.id)
            finally:
                logic._SAVING_TO_LIBRARY.reset(tok)
        for g, members in m.groups.items():
            kept = [i for i in members if i in saved_ids]
            if kept:
                data["groups"][g] = kept
    if "tiles" in kinds or "corpses" in kinds:
        for (tx, ty), cell in sorted(mp.tiles.items(),
                                     key=lambda kv: (kv[0][1], kv[0][0])):
            if not inside(tx, ty) or not isinstance(cell, dict):
                continue
            if "tiles" in kinds:
                tdata = {k: copy.deepcopy(v) for k, v in cell.items()
                         if k != "corpses"}
                if tdata:
                    data["tiles"].append([tx - xa, ty - ya, tdata])
            if "corpses" in kinds:
                for cid, corpse in sorted((cell.get("corpses") or {}).items()):
                    if not all(inside(*c)
                               for c in m.corpse_cells(tx, ty, corpse, mp)):
                        notes.append(f"⚠️ The corpse of `{cid}` was left "
                                     f"out: it reaches past the rectangle.")
                        continue
                    c = _relative_corpse(corpse, xa, ya)
                    if c.pop("maps", None):
                        notes.append(f"⚠️ The corpse of `{cid}` holds a "
                                     f"wrecked map; the prefab keeps the "
                                     f"corpse without it.")
                    data["corpses"].append([tx - xa, ty - ya, cid, c])
    if "zones" in kinds:
        for zname, z in sorted(mp.zones.items()):
            if not isinstance(z, dict):
                continue
            cells = [c for c in z.get("cells") or () if inside(*c)]
            anchor = z.get("anchor")
            if anchor and anchor not in saved_ids:
                continue          # an aura goes only with its anchor
            if not cells and not anchor:
                continue
            zd = Map._zone_to_dict(z)
            zd["cells"] = [[x - xa, y - ya] for (x, y) in sorted(cells)]
            data["zones"][zname] = zd
    data["definitions"] = _collect_definitions(mgr, m, data, definitions_all)
    for lib in list(data["definitions"]):
        if not data["definitions"][lib]:
            del data["definitions"][lib]
    return data, notes


def _relative_corpse(corpse: Dict[str, Any], ox: int, oy: int
                     ) -> Dict[str, Any]:
    """A corpse snapshot with its body's positions relative to (ox, oy)."""
    c = copy.deepcopy(corpse)
    for body in [c.get("entity")] + list(c.get("parts") or []):
        if isinstance(body, dict):
            body.pop("map", None)
            for k, o in (("x", ox), ("y", oy)):
                if isinstance(body.get(k), int):
                    body[k] -= o
            bv = body.get("vars")
            if isinstance(bv, dict):
                Match._shift_snake_path_vars(bv, -ox, -oy)
    return c


def _collect_definitions(mgr: Any, m: Match, data: Dict[str, Any],
                         every: bool) -> Dict[str, Dict[str, Any]]:
    """The definitions the contents use: the match-level ones the system
    lacks or holds differently, or with `every` all of them."""
    sysobj = mgr.systems.get(m.system_name) if mgr is not None else None

    def sys_lib(kind: str) -> Dict[str, Any]:
        return mgr.system_library(sysobj, kind) if sysobj is not None else {}

    out: Dict[str, Dict[str, Any]] = {k: {} for k in PREFAB_LIBRARIES}
    tnames = {t.get("_template") for _x, _y, t in data["tiles"]
              if isinstance(t.get("_template"), str)}
    for tn in sorted(tnames):
        tpl = m.tile_templates.get(tn)
        if tpl is None:
            continue
        mine = tpl.to_dict()
        if every or sys_lib("tile_templates").get(tn) != mine:
            out["tile_templates"][tn] = mine
    znames = {z.get("def") for z in data["zones"].values()
              if isinstance(z.get("def"), str)}
    for zn in sorted(znames):
        zd = m.zone_definitions.get(zn)
        if zd is None:
            continue
        mine = normalize_zone_def(zd)
        if every or normalize_zone_def(
                sys_lib("zone_definitions").get(zn) or {}) != mine \
                or zn not in sys_lib("zone_definitions"):
            out["zone_definitions"][zn] = mine
    snames: Set[str] = set()
    bodies = list(data["units"])
    for _x, _y, _cid, c in data["corpses"]:
        bodies += [c.get("entity")] + list(c.get("parts") or [])
    for b in bodies:
        if isinstance(b, dict) and isinstance(b.get("status"), dict):
            snames |= set(b["status"])
    # Statuses the units hold, and those the saved formulas and carried
    # definitions name: a quoted name in a hook, tick or function body (a
    # lava tile's status_apply(self, 'burn', ...)), a removes / blocked_by
    # token. What these carry can name more, so the scan repeats until
    # nothing new turns up; so do the `!func`s called.
    seen_s: Set[str] = set()
    seen_f: Set[str] = set()
    pending = set(snames)
    while True:
        for sn in sorted(pending - seen_s):
            seen_s.add(sn)
            sd = m.status_definitions.get(sn)
            if sd is None:
                continue
            if every or sys_lib("status_definitions").get(sn) != sd:
                out["status_definitions"][sn] = copy.deepcopy(sd)
        texts: List[str] = []
        _strings({k: v for k, v in data.items() if k != "definitions"},
                 texts)
        _strings(out, texts)
        for fname in sorted(_used_funcs(m, texts) - seen_f):
            seen_f.add(fname)
            fn = m.formula_functions[fname]
            texts.append(fn.body)
            mine = fn.to_dict()
            if every or sys_lib("formula_functions").get(fname) != mine:
                out["formula_functions"][fname] = mine
        pending = {sn for sn in m.status_definitions if sn not in seen_s
                   and any(_names_status(t, sn) for t in texts)}
        for sn in list(seen_s):
            sd = m.status_definitions.get(sn) or {}
            for k in ("removes", "blocked_by"):
                v = sd.get(k)
                toks = v if isinstance(v, list) \
                    else str(v).split(",") if v else []
                pending |= {str(t).strip() for t in toks
                            if str(t).strip() in m.status_definitions
                            and str(t).strip() not in seen_s}
        if not pending:
            return out


def _names_status(text: str, sname: str) -> bool:
    """Whether `text` names status `sname` as a quoted word (how formulas
    pass a status name to status_apply and the other status functions)."""
    return f"'{sname}'" in text or f'"{sname}"' in text


# ---- validation (import / load) ----------------------------------------------

def check_prefab(raw: Any, name: str) -> Dict[str, Any]:
    """`raw` as a prefab, or a VTTError naming what is wrong. The shape is
    checked here; what a unit holds is checked when a stamp spawns it."""
    if not isinstance(raw, dict) or raw.get("kind") != "prefab":
        raise VTTError(f"Prefab `{name}` must be a JSON object with "
                       f"\"kind\": \"prefab\".")
    extra = sorted(set(raw) - set(_PREFAB_KEYS))
    if extra:
        raise VTTError(f"Prefab `{name}`: unknown key(s) "
                       + ", ".join(f"`{k}`" for k in extra) + ".")
    d = copy.deepcopy(raw)
    w, h = d.get("width"), d.get("height")
    lim = int(logic.rule_max("max_grid_dimension") or 500)
    for k, v in (("width", w), ("height", h)):
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= lim:
            raise VTTError(f"Prefab `{name}`: {k} must be a whole number "
                           f"from 1 to {lim}.")

    def cell(dx: Any, dy: Any, what: str) -> None:
        if any(isinstance(v, bool) or not isinstance(v, int)
               for v in (dx, dy)) or not (0 <= dx < w and 0 <= dy < h):
            raise VTTError(f"Prefab `{name}`: {what} at ({dx},{dy}) is "
                           f"outside its {w}x{h} rectangle.")

    for key, typ in (("tiles", list), ("corpses", list), ("zones", dict),
                     ("units", list), ("groups", dict),
                     ("definitions", dict), ("var_names", dict)):
        d.setdefault(key, typ())
        if not isinstance(d[key], typ):
            raise VTTError(f"Prefab `{name}`: `{key}` must be a "
                           f"{'list' if typ is list else 'object'}.")
    if not isinstance(d.get("system"), str) or not d["system"]:
        raise VTTError(f"Prefab `{name}`: `system` must name the game "
                       f"system it was made for.")
    d["definitions_all"] = bool(d.get("definitions_all"))
    if "tags" in d:
        d["tags"] = logic.checked_tag_list(d["tags"], f"Prefab `{name}` tags",
                                           engine_ok=True)
        if not d["tags"]:
            del d["tags"]
    for t in d["tiles"]:
        if not (isinstance(t, list) and len(t) == 3 and isinstance(t[2], dict)):
            raise VTTError(f"Prefab `{name}`: a tile entry is [dx, dy, "
                           f"{{data}}].")
        cell(t[0], t[1], "a tile")
        if "corpses" in t[2]:
            raise VTTError(f"Prefab `{name}`: tile data can't hold "
                           f"`corpses` (corpses are listed under "
                           f"`corpses`).")
    for c in d["corpses"]:
        if not (isinstance(c, list) and len(c) == 4 and isinstance(c[2], str)
                and isinstance(c[3], dict)
                and isinstance(c[3].get("entity"), dict)):
            raise VTTError(f"Prefab `{name}`: a corpse entry is [dx, dy, "
                           f"id, {{\"entity\": ...}}].")
        cell(c[0], c[1], f"corpse `{c[2]}`")
        # A corpse is read by the roster and rebuilt by a revive: its body
        # and parts must be units (a malformed one crashed `!list`).
        parts = c[3].get("parts", [])
        if not isinstance(parts, list):
            raise VTTError(f"Prefab `{name}`: corpse `{c[2]}`'s `parts` "
                           f"must be a list of units.")
        for u in [c[3]["entity"]] + parts:
            _check_unit_dict(u, name, f"corpse `{c[2]}`")
    ids: Set[str] = set()
    for u in d["units"]:
        if not isinstance(u, dict) or not isinstance(u.get("id"), str) \
                or not u["id"]:
            raise VTTError(f"Prefab `{name}`: every unit is an entity "
                           f"object with an `id`.")
        if u["id"] in ids:
            raise VTTError(f"Prefab `{name}`: unit id `{u['id']}` is used "
                           f"twice.")
        ids.add(u["id"])
        cell(u.get("x"), u.get("y"), f"unit `{u['id']}`")
        _check_unit_dict(u, name, "the prefab")
    by_id = {u["id"]: u for u in d["units"]}
    for u in d["units"]:
        for link in ("part_of", "mounted_on"):
            if u.get(link) and u[link] not in ids:
                raise VTTError(f"Prefab `{name}`: unit `{u['id']}`'s "
                               f"{link} `{u[link]}` isn't a unit of the "
                               f"prefab.")
    # A part, mount or snake link that leads back to its unit (a unit that
    # is its own part, two riding each other) stamps a body nothing can
    # move or take apart.
    for link in ("part_of", "mounted_on", "__follows"):
        for u in d["units"]:
            seen = [u["id"]]
            cur = u
            while True:
                nxt = (cur.get("vars") or {}).get(link) \
                    if link.startswith("__") else cur.get(link)
                if not isinstance(nxt, str) or nxt not in by_id:
                    break
                if nxt in seen:
                    raise VTTError(
                        f"Prefab `{name}`: the `{link}` links of "
                        + " -> ".join(f"`{i}`" for i in seen + [nxt])
                        + " form a loop.")
                seen.append(nxt)
                cur = by_id[nxt]
    for zname, z in d["zones"].items():
        if not isinstance(z, dict):
            raise VTTError(f"Prefab `{name}`: zone `{zname}` must be an "
                           f"object.")
        for c in z.get("cells") or []:
            if not (isinstance(c, list) and len(c) == 2):
                raise VTTError(f"Prefab `{name}`: zone `{zname}` cells are "
                               f"[dx, dy] pairs.")
            cell(c[0], c[1], f"a cell of zone `{zname}`")
        if z.get("anchor") and z["anchor"] not in ids:
            raise VTTError(f"Prefab `{name}`: zone `{zname}` is anchored to "
                           f"`{z['anchor']}`, which isn't a unit of the "
                           f"prefab.")
    for g, members in d["groups"].items():
        if not isinstance(members, list) or any(i not in ids
                                                for i in members):
            raise VTTError(f"Prefab `{name}`: group `{g}` must list units "
                           f"of the prefab.")
    for lib, entries in d["definitions"].items():
        if lib not in PREFAB_LIBRARIES or not isinstance(entries, dict):
            raise VTTError(f"Prefab `{name}`: `definitions` holds "
                           + ", ".join(PREFAB_LIBRARIES) + " only.")
        for ename, entry in entries.items():
            if not isinstance(entry, dict):
                raise VTTError(f"Prefab `{name}`: definition "
                               f"`{lib}:{ename}` must be an object.")
            if lib == "tile_templates":
                logic.SpecialTileTemplate.from_dict(dict(entry, name=ename))
            elif lib == "formula_functions":
                logic.FormulaFunction.from_dict(entry)
            elif lib == "status_definitions":
                _check_status_definition(entry, name, ename)
            elif lib == "zone_definitions":
                logic.check_zone_def_name(ename)
                entries[ename] = logic.normalize_zone_def(entry)
    return d


def _check_unit_dict(u: Any, name: str, where: str) -> None:
    """A saved unit (a prefab unit, a corpse's body or part) the engine can
    read back: an id, a name, numeric x / y, vars and statuses as objects,
    and Entity.from_dict accepting it."""
    if not isinstance(u, dict) or not isinstance(u.get("id"), str) \
            or not u["id"]:
        raise VTTError(f"Prefab `{name}`: every unit of {where} is an "
                       f"entity object with an `id`.")
    who = f"Prefab `{name}`: unit `{u['id']}` of {where}"
    if not isinstance(u.get("name"), str):
        raise VTTError(f"{who} has no `name`.")
    for k in ("x", "y"):
        if isinstance(u.get(k), bool) or not isinstance(u.get(k), int):
            raise VTTError(f"{who}: `{k}` must be a whole number.")
    for k in ("vars", "status", "passives", "clamps"):
        if k in u and not isinstance(u[k], dict):
            raise VTTError(f"{who}: `{k}` must be an object.")
    try:
        Entity.from_dict(dict(u, map=None))
    except Exception as ex:  # noqa: BLE001 - any shape error
        raise VTTError(f"{who} isn't a unit: {ex}")


# The values a status definition's fields take (`!status stack|when|...`).
_STATUS_STACK_MODES = ("refresh", "add_level", "extend", "replace", "none")
_STATUS_TICK_WHENS = ("turn_end", "turn_start", "round_start", "round_end",
                      "never")


def _check_status_definition(entry: Dict[str, Any], name: str,
                             sname: str) -> None:
    """A carried status definition holds what `!status` would store: a
    stack mode and tick time it knows, a whole max_level, formulas and
    hooks as text, tags and tag filters that parse."""
    who = f"Prefab `{name}`: status definition `{sname}`"
    logic.check_status_name(sname)
    if entry.get("stack") not in (None, "", *_STATUS_STACK_MODES):
        raise VTTError(f"{who}: stack must be one of "
                       f"{', '.join(_STATUS_STACK_MODES)}.")
    if entry.get("tick_when") not in (None, "", *_STATUS_TICK_WHENS):
        raise VTTError(f"{who}: tick_when must be one of "
                       f"{', '.join(_STATUS_TICK_WHENS)}.")
    ml = entry.get("max_level")
    if ml is not None and (isinstance(ml, bool) or not isinstance(ml, int)
                           or ml < 0):
        raise VTTError(f"{who}: max_level must be a whole number >= 0.")
    for k in ("tick", "display"):
        if entry.get(k) is not None and not isinstance(entry[k], str):
            raise VTTError(f"{who}: `{k}` must be text.")
    hooks = entry.get("hooks", {})
    if not isinstance(hooks, dict) or any(
            k not in logic.STATUS_DEF_HOOKS.values() or not isinstance(v, str)
            for k, v in hooks.items()):
        raise VTTError(f"{who}: `hooks` maps "
                       + ", ".join(logic.STATUS_DEF_HOOKS.values())
                       + " to formula text.")
    if "data" in entry and not isinstance(entry["data"], dict):
        raise VTTError(f"{who}: `data` must be an object.")
    try:
        if "tags" in entry:
            entry["tags"] = logic.normalize_tags(entry["tags"])
        for k in ("removes", "blocked_by"):
            v = entry.get(k)
            toks = (v if isinstance(v, list)
                    else str(v).split(",") if v else [])
            for t in toks:
                t = str(t).strip()
                if t.startswith("tag:"):
                    logic.check_tag_filter(t[4:])
        for k in logic.ACTION_RESTRICTION_FIELDS:
            if k in entry and k.endswith("_tag"):
                logic.check_tag_filter(entry[k])
    except VTTError as ex:
        raise VTTError(f"{who}: {ex}")


# ---- geometry -----------------------------------------------------------------

class Turn:
    """The flip, then quarter turns clockwise, a stamp applies to a W x H
    prefab (0-based cells)."""

    def __init__(self, w: int, h: int, rotate: int = 0,
                 flip: Optional[str] = None) -> None:
        if rotate not in (0, 90, 180, 270):
            raise VTTError("rotate= takes 0, 90, 180 or 270 (degrees "
                           "clockwise).")
        if flip not in (None, "h", "v"):
            raise VTTError("flip= takes h (left-right) or v (top-bottom).")
        self.w, self.h, self.steps, self.flip = w, h, rotate // 90, flip

    @property
    def size(self) -> Tuple[int, int]:
        return (self.h, self.w) if self.steps % 2 else (self.w, self.h)

    def point(self, x: int, y: int) -> Tuple[int, int]:
        w, h = self.w, self.h
        if self.flip == "h":
            x = w - 1 - x
        elif self.flip == "v":
            y = h - 1 - y
        for _ in range(self.steps):
            x, y, w, h = h - 1 - y, x, h, w
        return x, y

    def rect(self, x: int, y: int, rw: int, rh: int
             ) -> Tuple[int, int, int, int]:
        """The rectangle (x, y, rw, rh) after the turn, as (x, y, w, h)."""
        a = self.point(x, y)
        b = self.point(x + rw - 1, y + rh - 1)
        return (min(a[0], b[0]), min(a[1], b[1]),
                abs(a[0] - b[0]) + 1, abs(a[1] - b[1]) + 1)

    def facing(self, f: Any) -> Any:
        if not isinstance(f, str) or not f:
            return f
        if self.flip == "h":
            f = Match._MIRROR_H.get(f, f)
        elif self.flip == "v":
            f = Match._MIRROR_V.get(f, f)
        for _ in range(self.steps * 2):
            f = rotate_direction(f, clockwise=True, eight_way=True)
        return f

    @property
    def swaps(self) -> bool:
        return self.steps % 2 == 1

    def path_vars(self, vars_: Dict[str, Any]) -> None:
        """Turn a snake head's trail cells (relative to the prefab)."""
        path = vars_.get("__seg_path")
        if isinstance(path, list):
            vars_["__seg_path"] = [list(self.point(p[0], p[1]))
                                   if isinstance(p, list) and len(p) == 2
                                   and all(isinstance(v, int) for v in p)
                                   else p for p in path]
        last = vars_.get("__seg_last")
        if isinstance(last, (list, tuple)) and len(last) == 2 \
                and all(isinstance(v, int) for v in last):
            vars_["__seg_last"] = list(self.point(last[0], last[1]))


def _var_name(d: Dict[str, Any], pf: Dict[str, Any], m: Match,
              rule: str) -> str:
    vn = d.get("var_names") if isinstance(d.get("var_names"), dict) else {}
    return str(vn.get(rule) or (pf.get("var_names") or {}).get(rule)
               or m.rules.get(rule))


def _dims(d: Dict[str, Any], pf: Dict[str, Any], m: Match) -> Tuple[int, int]:
    """A saved unit's (or corpse body's) footprint (w, h)."""
    vars_ = d.get("vars") if isinstance(d.get("vars"), dict) else {}
    out = []
    for rule in _FOOTPRINT_RULES:
        try:
            out.append(max(1, int(vars_.get(_var_name(d, pf, m, rule), 1))))
        except (TypeError, ValueError):
            out.append(1)
    return out[0], out[1]


def _turn_body(d: Dict[str, Any], pf: Dict[str, Any], m: Match,
               turn: Turn) -> None:
    """Turn one saved unit / corpse body in place: its anchor, footprint,
    facing and snake trail."""
    w, h = _dims(d, pf, m)
    x, y = int(d.get("x", 0)), int(d.get("y", 0))
    nx, ny, _nw, _nh = turn.rect(x, y, w, h)
    d["x"], d["y"] = nx, ny
    if "facing" in d:
        d["facing"] = turn.facing(d["facing"])
    vars_ = d.get("vars")
    if isinstance(vars_, dict):
        if turn.swaps:
            wv = _var_name(d, pf, m, "footprint_width_var")
            hv = _var_name(d, pf, m, "footprint_height_var")
            a, b = vars_.get(wv), vars_.get(hv)
            for k, v in ((wv, b), (hv, a)):
                if v is None:
                    vars_.pop(k, None)
                else:
                    vars_[k] = v
        turn.path_vars(vars_)


# ---- stamping -----------------------------------------------------------------

def _mint(base: str, taken: Set[str]) -> str:
    """An id from `base` that `taken` doesn't hold: `base`, `base2`, ...
    (Match.mint_entity_id's rule, over a given taken set)."""
    base = "".join(ch for ch in str(base).strip().lower().replace(" ", "_")
                   if ch.isalnum() or ch in ("_", "-")) or "unit"
    if base not in taken and base not in logic.RESERVED_IDS \
            and not logic.unreachable_id(base):
        return base
    n = 2
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"


def _keep_or_mint(oid: str, taken: Set[str]) -> str:
    """`oid` itself when free and usable as an id, else one minted from it."""
    if (oid not in taken and logic.ID_RE.fullmatch(oid)
            and not logic.unreachable_id(oid) and oid not in logic.RESERVED_IDS):
        return oid
    return _mint(_id_base(oid), taken)


def _id_base(oid: str) -> str:
    """The minting base of a saved id: its trailing digits dropped (`orc3`
    stamps as `orc`, `orc2`, ...)."""
    base = re.sub(r"\d+$", "", oid)
    return base or oid


def system_allows(mgr: Any, m: Match, pf: Dict[str, Any]) -> Optional[str]:
    """None when `pf` may be stamped into `m` (§8), else the refusal."""
    if pf.get("definitions_all"):
        return None
    want = pf.get("system")
    sysobj = mgr.systems.get(m.system_name)
    chain = [s.name for s in mgr.system_chain(sysobj)] if sysobj else []
    if want in chain:
        return None
    return (f"This prefab was made for game system `{want}`, and match "
            f"`{m.id}` runs on `{m.system_name}`"
            + (f" (inheriting from {', '.join(f'`{c}`' for c in chain[:-1])})"
               if len(chain) > 1 else "")
            + ". It can be stamped on that system or one inheriting from "
              "it; a prefab saved with `definitions=all` goes anywhere.")


def replace_victims(m: Match, mp: Map, x: int, y: int, w: int, h: int
                    ) -> Tuple[List[str], List[str]]:
    """What a replace stamp over (x, y, w, h) of `mp` removes: the units
    whose body covers a cell of it (whole bodies, riders included, as top
    ids) and the corpses lying in it."""
    def inside(cx: int, cy: int) -> bool:
        return x <= cx < x + w and y <= cy < y + h
    tops: List[str] = []
    seen: Set[str] = set()
    for e in sorted(m.entities.values(), key=lambda e: e.id):
        if e.map != mp.name or e.id in seen:
            continue
        top = m.body_root(e)
        while top.mounted_on and top.mounted_on in m.entities:
            top = m.body_root(m.entities[top.mounted_on])
        if top.id in seen:
            continue
        tree = _body_tree(m, top)
        if any(inside(*c) for u in tree for c in _unit_cells(m, u)):
            tops.append(top.id)
            seen |= {u.id for u in tree}
    corpses = [cid for (cx, cy), cell in mp.tiles.items()
               if isinstance(cell, dict) and inside(cx, cy)
               for cid in sorted(cell.get("corpses") or {})]
    return tops, sorted(corpses)


class NeedsConfirm(VTTError):
    """A replace stamp that would delete things, run without `confirm`."""


def stamp_prefab(mgr: Any, m: Match, name: str, pf: Dict[str, Any], x: int,
                 y: int, mp: Map, mode: Optional[str] = None,
                 rotate: int = 0, flip: Optional[str] = None,
                 team: Optional[str] = None, confirm: bool = False
                 ) -> Dict[str, Any]:
    """Stamp prefab `pf` (named `name`) with its top-left cell at (x, y) of
    map `mp`. All or nothing: any refusal leaves the match as it was.
    Returns {"units": new root ids, "all_units", "zones": new zone names,
    "renamed": {saved zone: new name}, "log": lines, "notes": ⚠️ lines}.
    A replace stamp that deletes something raises NeedsConfirm (its text
    lists what goes) unless `confirm`."""
    from action import _rollback_match
    mode = mode or str(m.rules.get("prefab_stamp_mode", "overlay"))
    if mode not in STAMP_MODES:
        raise VTTError(f"mode= takes {', '.join(STAMP_MODES)}.")
    refusal = system_allows(mgr, m, pf)
    if refusal:
        raise VTTError(refusal)
    turn = Turn(int(pf["width"]), int(pf["height"]), rotate, flip)
    W, H = turn.size
    if not (mp.in_bounds(x, y) and mp.in_bounds(x + W - 1, y + H - 1)):
        raise VTTError(f"Prefab `{name}` is {W}x{H}"
                       + (" turned" if turn.steps or turn.flip else "")
                       + f": placed at ({x},{y}) it reaches past the "
                         f"{mp.size_text()} map `{mp.name}`.")
    victims: List[str] = []
    gone_corpses: List[str] = []
    if mode == "replace":
        victims, gone_corpses = replace_victims(m, mp, x, y, W, H)
        if (victims or gone_corpses) and not confirm:
            bits = []
            if victims:
                bits.append("units " + ", ".join(f"`{v}`" for v in victims)
                            + " (with their parts and riders)")
            if gone_corpses:
                bits.append("the corpses of "
                            + ", ".join(f"`{c}`" for c in gone_corpses))
            raise NeedsConfirm(
                f"A replace stamp of `{name}` at ({x},{y}) clears its "
                f"{W}x{H} rectangle first, deleting " + " and ".join(bits)
                + ". Run it again with `confirm` to go ahead.")

    units = [copy.deepcopy(u) for u in pf.get("units") or []]
    for u in units:
        _turn_body(u, pf, m, turn)
    by_old = {u["id"]: u for u in units}

    # New ids: a unit keeps its saved id while that is free; a part named
    # after its root takes the root's new id as its prefix (`orc_head` of
    # `orc` -> `orc2_head`); anything else mints from its saved id without
    # trailing digits (`orc3` -> `orc`, `orc2`, ...).
    victim_ids: Set[str] = set()
    for v in victims:
        victim_ids |= {u.id for u in _body_tree(m, m.entities[v])}
    idmap: Dict[str, str] = {}
    minted: Set[str] = set()
    # Ids the units a replace removes hold are free for the stamp's own.
    free_taken = m._taken_entity_ids() - victim_ids

    def root_of(oid: str) -> str:
        seen = set()
        while by_old[oid].get("part_of") in by_old and oid not in seen:
            seen.add(oid)
            oid = by_old[oid]["part_of"]
        return oid

    for u in units:
        oid = u["id"]
        r = root_of(oid)
        if r != oid and r in idmap and oid.startswith(r + "_"):
            nid = _keep_or_mint(idmap[r] + oid[len(r):], free_taken | minted)
        else:
            nid = _keep_or_mint(oid, free_taken | minted)
        idmap[oid] = nid
        minted.add(nid)

    # Placement pre-check: every unit standing on its own cells must fit
    # on the map and on cells no unit outside the replaced ones holds.
    blockers: List[str] = []
    for u in units:
        located = (not u.get("part_of")) or (u.get("vars") or {}).get(
            "__part_located")
        if u.get("mounted_on") or not located:
            continue
        uv = u.get("vars") or {}
        w, h = _dims(u, pf, m)
        for dy in range(h):
            for dx in range(w):
                cx, cy = x + u["x"] + dx, y + u["y"] + dy
                if not mp.in_bounds(cx, cy):
                    raise VTTError(f"Stamped unit `{idmap[u['id']]}` would "
                                   f"reach past the map at ({cx},{cy}).")
                if uv.get("__cell_stackable"):
                    continue
                if mode != "replace":
                    occ = m.cell_occupant(cx, cy, tuple(victim_ids), mp)
                    if occ is not None:
                        occ = m.body_root(m.entities[occ]).id
                        if occ not in blockers:
                            blockers.append(occ)
    if blockers:
        raise VTTError(f"Prefab `{name}` can't be stamped at ({x},{y}): "
                       + ", ".join(f"`{b}`" for b in blockers)
                       + " stand(s) where its units go. Move them, stamp "
                         "elsewhere, or use `mode=replace`.")
    m.check_unit_room(len(units) - len(
        [i for i in victim_ids if i in m.entities]), f"prefab `{name}`")

    pre = m.to_dict(include_history=False)
    log: List[str] = []
    notes: List[str] = []
    renamed: Dict[str, str] = {}
    new_zones: List[str] = []
    try:
        if mode == "replace":
            _clear_rect(m, mp, x, y, W, H, victims, log)
        _merge_definitions(m, pf, notes)
        _stamp_tiles(m, mp, pf, turn, x, y, mode)
        _stamp_corpses(m, mp, pf, turn, x, y, idmap, notes)
        roots = _spawn_units(m, mp, pf, units, idmap, x, y, team, log)
        new_zones = _stamp_zones(m, mp, pf, turn, x, y, idmap, renamed)
        for g, members in (pf.get("groups") or {}).items():
            for oid in members:
                if idmap.get(oid) in m.entities:
                    m.add_to_group(g, idmap[oid])
        all_units = [idmap[u["id"]] for u in units
                     if idmap.get(u["id"]) in m.entities]
        log += m.emit_event("prefab_stamped", {
            "prefab": name, "x": x, "y": y, "map": mp.name,
            "units": list(all_units), "zones": list(new_zones)})
    except Exception:
        _rollback_match(m, None, pre)
        raise
    return {"units": roots, "all_units": all_units, "zones": new_zones,
            "renamed": renamed, "log": log, "notes": notes,
            "removed": victims, "size": (W, H)}


def _clear_rect(m: Match, mp: Map, x: int, y: int, w: int, h: int,
                victims: List[str], log: List[str]) -> None:
    """A replace stamp's clearing: the victims despawned, tile data and
    corpses of the rectangle wiped, zones lose their cells there (a zone
    left without cells is removed, unless it is an aura)."""
    for v in victims:
        if v not in m.entities:
            continue
        # Riders first: despawning a vehicle ejects its riders, and a
        # replace deletes the whole body (parts go with their root).
        for u in reversed(_body_tree(m, m.entities[v])):
            if not u.part_of and u.id in m.entities:
                log.extend(u.remove(despawn=True))
    for cx in range(x, x + w):
        for cy in range(y, y + h):
            mp.tiles.pop((cx, cy), None)
    for zname in list(mp.zones):
        z = mp.zones[zname]
        cells = z.get("cells") or set()
        keep = {c for c in cells
                if not (x <= c[0] < x + w and y <= c[1] < y + h)}
        if keep != cells:
            z["cells"] = keep
            if not keep and not z.get("anchor"):
                del mp.zones[zname]


def _merge_definitions(m: Match, pf: Dict[str, Any],
                       notes: List[str]) -> None:
    """A prefab's definitions into the match: a missing one is added, one
    that exists with other content keeps the match's, with a ⚠️."""
    defs = pf.get("definitions") or {}
    differs: List[str] = []
    for tn, entry in (defs.get("tile_templates") or {}).items():
        tpl = logic.SpecialTileTemplate.from_dict(dict(entry, name=tn))
        if tn not in m.tile_templates:
            m.tile_templates[tn] = tpl
        elif m.tile_templates[tn].to_dict() != tpl.to_dict():
            differs.append(f"tile template `{tn}`")
    for zn, entry in (defs.get("zone_definitions") or {}).items():
        entry = normalize_zone_def(entry)
        if zn not in m.zone_definitions:
            m.zone_definitions[zn] = entry
        elif normalize_zone_def(m.zone_definitions[zn]) != entry:
            differs.append(f"zone definition `{zn}`")
    for sn, entry in (defs.get("status_definitions") or {}).items():
        if sn not in m.status_definitions:
            m.status_definitions[sn] = copy.deepcopy(entry)
        elif m.status_definitions[sn] != entry:
            differs.append(f"status definition `{sn}`")
    for fn, entry in (defs.get("formula_functions") or {}).items():
        f = logic.FormulaFunction.from_dict(entry)
        if fn not in m.formula_functions:
            m.formula_functions[fn] = f
        elif m.formula_functions[fn].to_dict() != f.to_dict():
            differs.append(f"function `{fn}`")
    if differs:
        notes.append("⚠️ The match already has its own " + ", ".join(differs)
                     + ", which differ from the prefab's; the match's are "
                       "kept.")


def _turn_facing(d: Dict[str, Any], turn: Turn) -> None:
    """Turn a stamped tile's / zone's `facing` (#257). One with no facing
    stays without one (it has no direction to turn)."""
    if turn.steps == 0 and turn.flip is None:
        return
    f = d.get(logic.FACING_FIELD)
    if isinstance(f, str) and f in logic.ALLOWED_DIRECTIONS:
        d[logic.FACING_FIELD] = turn.facing(f)


def _stamp_tiles(m: Match, mp: Map, pf: Dict[str, Any], turn: Turn,
                 x: int, y: int, mode: str) -> None:
    for dx, dy, tdata in pf.get("tiles") or []:
        nx, ny = turn.point(dx, dy)
        cx, cy = x + nx, y + ny
        old = mp.tiles.get((cx, cy))
        has_data = isinstance(old, dict) and any(k != "corpses" for k in old)
        if mode == "keep" and has_data:
            continue
        new = copy.deepcopy(tdata)
        _turn_facing(new, turn)
        if isinstance(old, dict) and old.get("corpses"):
            new["corpses"] = old["corpses"]
        mp.tiles[(cx, cy)] = new


def _stamp_corpses(m: Match, mp: Map, pf: Dict[str, Any], turn: Turn,
                   x: int, y: int, idmap: Dict[str, str],
                   notes: List[str]) -> None:
    taken = set(idmap.values())
    for dx, dy, cid, corpse in pf.get("corpses") or []:
        c = copy.deepcopy(corpse)
        ent = c["entity"]
        ent["x"], ent["y"] = dx, dy
        # The corpse lies on its body's anchor cell after the turn.
        _turn_body(ent, pf, m, turn)
        nid = _keep_or_mint(cid, m._taken_entity_ids() | taken)
        taken.add(nid)
        cmap = {cid: nid}
        for p in c.get("parts") or []:
            if isinstance(p, dict) and isinstance(p.get("id"), str):
                pid = p["id"]
                want = nid + pid[len(cid):] if pid.startswith(cid + "_") \
                    else pid
                cmap[pid] = _keep_or_mint(want, m._taken_entity_ids() | taken)
                taken.add(cmap[pid])
        ent["id"] = nid
        for p in c.get("parts") or []:
            if not isinstance(p, dict):
                continue
            _turn_body(p, pf, m, turn)
            p["id"] = cmap.get(p.get("id"), p.get("id"))
            if p.get("part_of") in cmap:
                p["part_of"] = cmap[p["part_of"]]
            pv = p.get("vars")
            if isinstance(pv, dict) and pv.get("__follows") in cmap:
                pv["__follows"] = cmap[pv["__follows"]]
        for body in [ent] + [p for p in c.get("parts") or []
                             if isinstance(p, dict)]:
            body["map"] = mp.name
            body["x"] += x
            body["y"] += y
            bv = body.get("vars")
            if isinstance(bv, dict):
                Match._shift_snake_path_vars(bv, x, y)
        if isinstance(c.get("groups"), dict):
            c["groups"] = {cmap.get(k, k): v for k, v in c["groups"].items()}
        mp.tile_data(ent["x"], ent["y"]).setdefault("corpses", {})[nid] = c


def _spawn_units(m: Match, mp: Map, pf: Dict[str, Any],
                 units: List[Dict[str, Any]], idmap: Dict[str, str],
                 x: int, y: int, team: Optional[str],
                 log: List[str]) -> List[str]:
    """Spawn the turned unit dicts (parents and vehicles first) at their
    cells, links remapped, then lay glued / region parts and riders on
    their bodies. Returns the new top-level ids."""
    roots: List[str] = []
    for u in units:
        d = copy.deepcopy(u)
        oid = d["id"]
        d["id"] = idmap[oid]
        d.pop("map", None)
        vehicle = None
        if d.get("part_of"):
            d["part_of"] = idmap[d["part_of"]]
        if d.get("mounted_on"):
            vehicle = idmap[d["mounted_on"]]
            d["mounted_on"] = vehicle
        vars_ = d.get("vars") if isinstance(d.get("vars"), dict) else {}
        fol = vars_.get("__follows")
        if isinstance(fol, str):
            if fol in idmap:
                vars_["__follows"] = idmap[fol]
            else:
                vars_.pop("__follows", None)
        Match._shift_snake_path_vars(vars_, x, y)
        if team is not None:
            vars_[_var_name(d, pf, m, "team_var")] = str(team)
        ne = Entity.from_dict(d)
        ne.map = None
        token = logic.PLACEMENT_IGNORE.set((vehicle,) if vehicle else ())
        try:
            _, slog = ne.spawn(m, x + int(u["x"]), y + int(u["y"]),
                               map_name=mp.name)
        finally:
            logic.PLACEMENT_IGNORE.reset(token)
        log.extend(slog)
        ne2 = m.entities.get(d["id"])
        want = d.get("facing")
        if ne2 is not None and want and ne2.facing != want:
            # A spawn faces the spawn_face_toward_center way; a stamped unit
            # keeps the facing it was saved with (turned with the prefab).
            try:
                m.set_entity_facing(ne2, want)
            except VTTError as ex:
                log.append(f"⚠️ `{ne2.id}` keeps facing {ne2.facing}: {ex}")
        if not d.get("part_of") and not d.get("mounted_on"):
            roots.append(d["id"])
    for u in units:
        nid = idmap[u["id"]]
        if nid in m.entities and not u.get("part_of"):
            m._restamp_parts_for(nid)
    for u in units:
        nid = idmap[u["id"]]
        if nid in m.entities and any(v.get("mounted_on") == u["id"]
                                     for v in units):
            m._restamp_riders_for(nid)
    return roots


def _stamp_zones(m: Match, mp: Map, pf: Dict[str, Any], turn: Turn,
                 x: int, y: int, idmap: Dict[str, str],
                 renamed: Dict[str, str]) -> List[str]:
    made: List[str] = []
    for zname, zd in sorted((pf.get("zones") or {}).items()):
        z = mp._zone_from_dict(dict(zd, cells=[]))
        if z is None:
            continue
        cells = set()
        for c in zd.get("cells") or []:
            nx, ny = turn.point(c[0], c[1])
            if mp.in_bounds(x + nx, y + ny):
                cells.add((x + nx, y + ny))
        z["cells"] = cells
        _turn_facing(z, turn)
        new = m.mint_zone_name(zname)
        if new != zname:
            renamed[zname] = new
        anchor = zd.get("anchor")
        if anchor:
            z["anchor"] = idmap.get(anchor)
            if z["anchor"] not in m.entities:
                continue
        mp.zones[new] = z
        if anchor:
            m._stamp_anchored_zone(new, mp)
        made.append(new)
    return made
