"""Reactions (#36): the exchange an action opens when units may react to it.

The design is docs/damage_reactions_design.md. A typed `!action` whose
action someone can react to runs in stages, each its own command:

1. `begin` (the `!action` command): the action's `prepare` runs (checks
   first, as for any action), the units it affects are read (`affects`,
   default its unit targets), each gets its own copy of `prepare`'s locals
   (`incoming`), and the BEFORE window opens for every unit holding a
   reaction that is available now. Auto-mode units pick at once. When no
   unit is left to answer, the action resolves in the same command.
2. `!react <unit> <reaction> ...` / `!react <unit> pass` / `!react skip`:
   picks, passes and the host's skip; the command that closes the window
   resolves it.
3. Resolution: the picked before halves run in one order, then the action
   is read again (affects, targets, redirects), then the action's cost and
   body run (unless a reaction cancelled it), then the AFTER window opens
   for the after-only reactions of the units the action reached and the
   bystanders, and once it closes every after half runs in one order.

The open exchange is `Match.reaction_exchange` (JSON, saved, undo state);
every value in it is storable. The wall clock of the open window is
runtime (`Match._reaction_opened`).

Not built yet (design §8, §3): reacting to a reaction (a reaction's halves
open no windows), `react_point` mid-body windows, Discord buttons.
"""
from __future__ import annotations

import dataclasses
import time
from typing import Any, Dict, List, Optional, Tuple

from logic import (VTTError, UnitFlags, jcopy, _own_value,
                   tag_filter_matches, ACTION_IMMUNE_FIELDS,
                   ACTION_FORBID_FIELDS, FORMULA_TEXT_HIDDEN,
                   JOURNAL_AS_STATE)

# The control keys of an `incoming` copy (design §4): written by reactions,
# never locals of `prepare` / `body`.
CONTROL_KEYS = ("cancel", "avoid", "cancelled", "cancelled_by")
READ_ONLY_KEYS = ("cancelled", "cancelled_by")
# How many reaction uses a match remembers for reactions_used().
REACTION_LOG_MAX = 2000
# reactions_used(since=...) words.
SINCE_WORDS = ("own_turn", "round", "turn", "action", "ever")
MODES = ("ask", "auto", "never")


# ----------------------------------------------------------------------
# incoming
# ----------------------------------------------------------------------

class IncomingProxy:
    """`incoming` / `incoming_of(eid)`: one unit's copy of the incoming
    action's locals, read through the match's open exchange each time (a
    reaction half that fails rolls the match back, exchange included).
    A key nobody set reads None. Writable in before halves and the body;
    read-only in conditions and after halves."""

    __slots__ = ("_m", "_key", "_writable")

    def __init__(self, match: Any, key: str, writable: bool) -> None:
        object.__setattr__(self, "_m", match)
        object.__setattr__(self, "_key", key)
        object.__setattr__(self, "_writable", writable)

    def _copy(self) -> Dict[str, Any]:
        ex = self._m.reaction_exchange
        if not isinstance(ex, dict):
            raise AttributeError("no action is resolving its reactions now")
        return ex.setdefault("incoming", {}).setdefault(self._key, {})

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        ex = self._m.reaction_exchange or {}
        if name == "cancelled":
            return bool(ex.get("cancelled"))
        if name == "cancelled_by":
            by = ex.get("cancelled_by")
            return list(by) if isinstance(by, list) else None
        return jcopy(self._copy().get(name))

    def __setattr__(self, name: str, value: Any) -> None:
        if not self._writable:
            raise AttributeError(
                "`incoming` is read-only here: an after half and a "
                "condition read it, the body has already used it.")
        if name in READ_ONLY_KEYS:
            raise AttributeError(f"`incoming.{name}` is the engine's to "
                                 f"set: read it, don't write it.")
        if name.startswith("_"):
            raise AttributeError(f"`incoming.{name}`: a key can't start "
                                 f"with `_`.")
        from action import Coord
        if isinstance(value, Coord):
            value = value.store_value()
        try:
            self._copy()[name] = _own_value(value)
        except VTTError as ex:
            raise AttributeError(f"`incoming.{name}`: {ex}")

    def __repr__(self) -> str:
        return f"incoming({self._key or 'action'})"


def _storable_locals(src: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """`prepare`'s locals as the window can hold them (JSON values), and
    the names it couldn't (a proxy, a function)."""
    from action import Coord
    out: Dict[str, Any] = {}
    dropped: List[str] = []
    for k, v in src.items():
        if not isinstance(k, str) or k.startswith("_"):
            continue
        if isinstance(v, Coord):
            v = v.store_value()
        elif isinstance(v, list) and any(isinstance(c, Coord) for c in v):
            v = [c.store_value() if isinstance(c, Coord) else c for c in v]
        try:
            out[k] = _own_value(v)
        except Exception:      # noqa: BLE001 - named in the reply
            dropped.append(k)
    return out, dropped


def _plain(copy: Dict[str, Any]) -> Dict[str, Any]:
    """A copy without its control keys: the locals a body starts with."""
    return {k: jcopy(v) for k, v in copy.items() if k not in CONTROL_KEYS}


# ----------------------------------------------------------------------
# Targets
# ----------------------------------------------------------------------

def target_to_json(target_type: str, value: Any) -> Any:
    from action import Coord

    def one(v: Any) -> Any:
        if isinstance(v, Coord):
            return [v.x, v.y, v.map, bool(v.named)]
        return v
    if isinstance(value, list):
        return [one(v) for v in value]
    return one(value)


def target_from_json(target_type: str, value: Any) -> Any:
    from action import Coord

    def one(v: Any) -> Any:
        if target_type.startswith("location") and isinstance(v, list) \
                and len(v) == 4:
            return Coord(v[0], v[1], v[2], named=bool(v[3]))
        return v
    if target_type.endswith("_list") and isinstance(value, list):
        return [one(v) for v in value]
    return one(value)


def unit_targets(target_type: str, value: Any) -> List[str]:
    if target_type == "entity" and isinstance(value, str):
        return [value]
    if target_type == "entity_list" and isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


# ----------------------------------------------------------------------
# Who holds reactions
# ----------------------------------------------------------------------

def _mode(m: Any, e: Any) -> str:
    own = e.vars.get("__reaction_mode")
    if isinstance(own, str) and own.strip().lower() in MODES:
        return own.strip().lower()
    rule = str(m.rules.get("reaction_mode", "ask")).lower()
    return rule if rule in MODES else "ask"


def _window_limit(m: Any, e: Any) -> int:
    own = e.vars.get("__reactions_per_window")
    if isinstance(own, int) and not isinstance(own, bool) \
            and (own >= 1 or own == -1):
        return own
    try:
        return int(m.rules.get("reactions_per_window", 1))
    except (TypeError, ValueError):
        return 1


def _candidates(m: Any) -> List[str]:
    """Living units that may hold a reaction: those whose vars hold one,
    and the riders of a unit holding one (a seat's reactions)."""
    holders = m._uflags.holders_of(m, UnitFlags.REACTIONS)
    out: List[str] = []
    for eid in holders:
        e = m.entities.get(eid)
        if e is None:
            continue
        out.append(eid)
    if holders:
        for e in m.entities.values():
            if e.is_mounted and e.mounted_on in holders \
                    and e.id not in out:
                out.append(e.id)
    alive = [eid for eid in out if m.entities[eid].is_alive]
    order = {eid: i for i, eid in enumerate(m.turn_order)}
    return sorted(alive, key=lambda eid: (order.get(eid, len(order)), eid))


def unit_reactions(m: Any, e: Any) -> List[Tuple[Any, Optional[str]]]:
    """(Action, vehicle id or None) of every reaction `e` holds: its own
    (a vehicle's seats excepted: they are its riders') and its seat's."""
    from action import discover_actions, discover_mount_actions
    out: List[Tuple[Any, Optional[str]]] = []
    for acts in discover_actions(e, m.rules).values():
        for a in acts:
            if a.reaction is not None and not (
                    a.container_path == "slots"
                    or a.container_path.startswith("slots.")):
                out.append((a, None))
    if e.is_mounted:
        for acts in discover_mount_actions(e, m, m.rules).values():
            for a in acts:
                if a.reaction is not None:
                    out.append((a, e.mounted_on))
    return out


def mount_routing(m: Any, rider_id: str, action: Any,
                  vehicle_id: Optional[str]) -> Tuple[str, Optional[Dict[str, Any]], Any]:
    """(acting unit, extra ctx, action) for a unit using `action`, through
    its seat when `vehicle_id` is set, as `!action` routes a rider's seat
    action (mount_action_actor)."""
    if not vehicle_id:
        return rider_id, None, action
    veh = m.entities.get(vehicle_id)
    mode = (veh.vars.get("mount_action_actor") if veh is not None else None) \
        or m.rules.get("mount_action_actor", "rider")
    rider = m.entities.get(rider_id)
    extra = {"vehicle": vehicle_id, "rider": rider_id,
             "slot": rider.mount_slot if rider is not None else None}
    if str(mode) == "vehicle":
        return vehicle_id, extra, action
    return rider_id, extra, dataclasses.replace(action, container_path="")


def _ancestors(m: Any, eid: str) -> List[str]:
    out: List[str] = []
    e = m.entities.get(eid)
    seen = set()
    while e is not None and e.part_of and e.part_of not in seen:
        seen.add(e.part_of)
        out.append(e.part_of)
        e = m.entities.get(e.part_of)
    return out


def copy_key(m: Any, ex: Dict[str, Any], uid: str) -> str:
    """Whose `incoming` copy unit `uid` reads: its own, else the copy of a
    body part of it the action affects (reactions_cover_parts), else the
    action's (a bystander)."""
    copies = ex.get("incoming") or {}
    if uid in copies:
        return uid
    if m.rules.get("reactions_cover_parts", True):
        for key in copies:
            if key and uid in _ancestors(m, key):
                return key
    return ""


def _covered(m: Any, ex: Dict[str, Any], uid: str,
             reached: List[str]) -> bool:
    if uid in reached:
        return True
    if m.rules.get("reactions_cover_parts", True):
        return any(uid in _ancestors(m, a) for a in reached)
    return False


def _reached(ex: Dict[str, Any]) -> List[str]:
    """Every unit the action affected at some point (the after window)."""
    out: List[str] = []
    for key in ("affected", "first_affected", "dropped"):
        for u in ex.get(key) or []:
            if u not in out:
                out.append(u)
    return out


def unit_outcome(ex: Dict[str, Any], uid: str, m: Any) -> str:
    """`resolved` or `cancelled`: how the action ended for unit `uid` (a
    unit that left it, or a part of one, sees it as cancelled)."""
    if ex.get("outcome") != "resolved":
        return "cancelled"
    dropped = ex.get("dropped") or []
    if uid in dropped or any(uid in _ancestors(m, d) for d in dropped):
        return "cancelled"
    return "resolved"


def _eval_ctx(m: Any, ex: Dict[str, Any], uid: str, writable: bool,
              extra: Optional[Dict[str, Any]] = None):
    from formula import EvalCtx
    ttype = ex["target_type"]
    extras = {
        "actor": ex["eff_actor"],
        "target": target_from_json(ttype, jcopy(ex.get("target"))),
        "affected": list(ex.get("affected") or []),
        "incoming": IncomingProxy(m, copy_key(m, ex, uid), writable),
        "action_name": ex["name"],
        "action_tags": list(ex.get("tags") or []),
        "args": dict(ex.get("args") or {}),
    }
    if extra:
        extras.update(extra)
    return EvalCtx(this=m.current_entity_id(), target=uid, extras=extras)


def _formula_error(ex: BaseException) -> str:
    if FORMULA_TEXT_HIDDEN.get():
        return "its formula failed"
    return f"its formula failed: {ex}"


def _reaction_open(m: Any, ex: Dict[str, Any], uid: str, act: Any,
                   phase: str, notes: List[str]) -> bool:
    """Whether unit `uid` may pick reaction `act` in the `phase` window now
    (design §2, §3 step 2)."""
    from formula import FormulaEngine
    spec = act.reaction
    if spec is None or spec.disabled or spec.reacts_to_error:
        return False
    if phase == "before":
        if not spec.before:
            return False
    else:
        if spec.before or not spec.after:
            return False
        want = spec.after_on
        if want != "any" and want != unit_outcome(ex, uid, m):
            return False
    if spec.reacts_to is not None:
        try:
            if not tag_filter_matches(ex.get("tags") or [], spec.reacts_to):
                return False
        except VTTError:
            return False
    if uid in (ex["actor"], ex["eff_actor"]) and not spec.self_trigger:
        return False
    if spec.reacts_on == "target":
        reached = (list(ex.get("affected") or []) if phase == "before"
                   else _reached(ex))
        if not _covered(m, ex, uid, reached):
            return False
    if m.action_restrictions(ACTION_FORBID_FIELDS,
                             m.action_forbid_units(uid), act.name,
                             act.tags, uid):
        return False
    if spec.condition:
        try:
            ok = FormulaEngine(m).eval_expression(
                spec.condition, _eval_ctx(m, ex, uid, False))
        except Exception as err:      # noqa: BLE001 - a note, not raised
            notes.append(m.unit_tag(uid) + f"⚠️ `{uid}`'s reaction "
                         f"`{act.name}` isn't offered: its condition "
                         f"failed ({_formula_error(err)}).")
            return False
        if uid not in m.entities:
            return False
        return bool(ok)
    return True


def available(m: Any, ex: Dict[str, Any], uid: str, phase: str,
              notes: Optional[List[str]] = None,
              ) -> List[Tuple[Any, Optional[str]]]:
    """The reactions unit `uid` may pick now in the `phase` window, minus
    those it has picked already."""
    e = m.entities.get(uid)
    if e is None or not e.is_alive:
        return []
    picked = {p["path"] for p in ex.get("picks") or []
              if p["unit"] == uid and p.get("phase") == phase}
    notes = notes if notes is not None else []
    return [(a, via) for a, via in unit_reactions(m, e)
            if a.full_path not in picked
            and _reaction_open(m, ex, uid, a, phase, notes)]


def effective_priority(m: Any, act: Any, half: str,
                       continuation: bool = False) -> float:
    spec = act.reaction
    if half == "before":
        p = spec.before_priority if spec.before_priority is not None \
            else spec.priority
        if spec.cancels:
            p += int(m.rules.get("reaction_cancel_priority", 1000))
    else:
        p = spec.after_priority if spec.after_priority is not None \
            else spec.priority
        if continuation:
            p += int(m.rules.get("reaction_continuation_priority", 500))
    return p


# ----------------------------------------------------------------------
# Windows
# ----------------------------------------------------------------------

def _name(m: Any, eid: str) -> str:
    """A unit's name as each channel's view sees it (a say() name marker);
    the id in backticks for a unit gone without a corpse."""
    shown = m.say_name(eid)
    return f"`{eid}`" if shown == eid else shown


def _pick(m: Any, ex: Dict[str, Any], uid: str, act: Any,
          via: Optional[str], phase: str, target: Any = None,
          args: Optional[Dict[str, Any]] = None,
          answers: Optional[List[Any]] = None, auto: bool = False) -> None:
    n = int(ex.get("pick_seq", 0)) + 1
    ex["pick_seq"] = n
    ex.setdefault("picks", []).append({
        "n": n, "unit": uid, "path": act.full_path, "name": act.name,
        "via": via, "phase": phase,
        "target": target_to_json(act.target_type, target),
        "target_type": act.target_type,
        "args": dict(args or {}), "answers": list(answers or []),
        "auto": auto,
    })
    st = ex.setdefault("units", {}).setdefault(uid, {"done": False,
                                                       "picked": 0})
    st["picked"] = int(st.get("picked", 0)) + 1


def _auto_picks(m: Any, ex: Dict[str, Any], uid: str, phase: str,
                options: List[Tuple[Any, Optional[str]]],
                notes: List[str]) -> List[str]:
    """Auto mode: each available reaction whose auto_when holds (or that
    has none), highest priority first, up to the per-window limit. Only
    reactions without a target of their own are picked."""
    from formula import FormulaEngine
    e = m.entities[uid]
    limit = _window_limit(m, e)
    ranked = sorted(enumerate(options), key=lambda io: (
        -effective_priority(m, io[1][0], phase if phase == "before"
                            else "after"), io[0]))
    names: List[str] = []
    for _i, (act, via) in ranked:
        if limit != -1 and len(names) >= limit:
            break
        if act.target_type != "none":
            continue
        spec = act.reaction
        if spec.auto_when:
            try:
                ok = FormulaEngine(m).eval_expression(
                    spec.auto_when, _eval_ctx(m, ex, uid, False))
            except Exception as err:      # noqa: BLE001
                notes.append(m.unit_tag(uid) + f"⚠️ `{uid}`'s reaction "
                             f"`{act.name}`: its auto_when failed "
                             f"({_formula_error(err)}); not picked.")
                continue
            if not ok:
                continue
        _pick(m, ex, uid, act, via, phase, auto=True)
        names.append(act.name)
    return names


def open_window(m: Any, ex: Dict[str, Any], phase: str,
                notes: List[str]) -> List[str]:
    """Fill the `phase` window's units (fixed from now on), make the auto
    picks, and return the lines of each unit still to answer."""
    ex["phase"] = phase
    ex["units"] = {}
    ex["unit_order"] = []
    lines: List[str] = []
    for uid in _candidates(m):
        e = m.entities[uid]
        mode = _mode(m, e)
        if mode == "never":
            continue
        opts = available(m, ex, uid, phase, notes)
        if not opts:
            continue
        ex["units"][uid] = {"done": False, "picked": 0}
        ex["unit_order"].append(uid)
        if mode == "auto":
            names = _auto_picks(m, ex, uid, phase, opts, notes)
            ex["units"][uid]["done"] = True
            lines.append(m.unit_tag(uid) + (
                f"🤖 {_name(m, uid)} reacts with "
                + ", ".join(f"`{n}`" for n in names) + "."
                if names else f"🤖 {_name(m, uid)} doesn't react."))
            continue
        lines.append(_option_line(m, ex, uid, opts))
    return lines


def _option_line(m: Any, ex: Dict[str, Any], uid: str,
                 opts: List[Tuple[Any, Optional[str]]]) -> str:
    def cmd(act: Any) -> str:
        tail = {"none": "", "entity": " <unit>", "entity_list":
                " <unit ...>", "location": " <x> <y>", "location_list":
                " <x> <y> ...", "corpse": " <corpse>", "corpse_list":
                " <corpse ...>"}.get(act.target_type, "")
        return f"`!react {uid} {act.name}{tail}`"
    names = ", ".join(cmd(a) for a, _v in opts)
    return (m.unit_tag(uid) + f"- {_name(m, uid)} can react: {names} — or "
            f"`!react {uid} pass`.")


def waiting(ex: Dict[str, Any]) -> List[str]:
    return [u for u in ex.get("unit_order") or []
            if not (ex.get("units") or {}).get(u, {}).get("done")]


def _refresh_done(m: Any, ex: Dict[str, Any]) -> None:
    """A unit gone, dead, at its limit or with nothing left is done."""
    for uid in waiting(ex):
        e = m.entities.get(uid)
        st = ex["units"][uid]
        if e is None or not e.is_alive:
            st["done"] = True
            continue
        lim = _window_limit(m, e)
        if lim != -1 and int(st.get("picked", 0)) >= lim:
            st["done"] = True
            continue
        if not available(m, ex, uid, ex["phase"]):
            st["done"] = True


def _action_line(m: Any, ex: Dict[str, Any]) -> str:
    targets = unit_targets(ex["target_type"], ex.get("target"))
    on = (" on " + ", ".join(_name(m, t) for t in targets)) if targets else ""
    return f"{_name(m, ex['actor'])}'s `{ex['name']}`{on}"


def window_text(m: Any, ex: Dict[str, Any]) -> List[str]:
    """`!react`: the open window, its picks and who is still to answer."""
    phase = ex.get("phase", "before")
    lines = [f"⏳ Reactions to {_action_line(m, ex)} ("
             f"{'before' if phase == 'before' else 'after'} it resolves):"]
    for p in ex.get("picks") or []:
        if p.get("phase") != phase:
            continue
        lines.append(m.unit_tag(p["unit"]) + f"- {_name(m, p['unit'])} "
                     f"picked `{p['name']}`.")
    for uid in waiting(ex):
        opts = available(m, ex, uid, phase)
        if opts:
            lines.append(_option_line(m, ex, uid, opts))
    if not waiting(ex):
        lines.append("Nobody is still to answer.")
    lines.append("A host closes the window with `!react skip`.")
    return lines


# ----------------------------------------------------------------------
# Running the action and the halves
# ----------------------------------------------------------------------

class _Runtime:
    """m._runtime_ctx / _runtime_mgr while something runs (use_action)."""

    def __init__(self, m: Any, ctx: Any, mgr: Any) -> None:
        self.m, self.ctx, self.mgr = m, ctx, mgr

    def __enter__(self):
        self.prev = (getattr(self.m, "_runtime_ctx", None),
                     getattr(self.m, "_runtime_mgr", None))
        self.m._runtime_ctx, self.m._runtime_mgr = self.ctx, self.mgr
        return self

    def __exit__(self, *_a):
        self.m._runtime_ctx, self.m._runtime_mgr = self.prev


def _find_action(m: Any, holder: str, path: str, via: Optional[str] = None):
    """The action at `path` on unit `holder` (through its seat on vehicle
    `via`), or None when it is gone."""
    from action import discover_actions, discover_mount_actions
    e = m.entities.get(holder)
    if e is None:
        return None
    if via:
        if e.mounted_on != via:
            return None
        found = discover_mount_actions(e, m, m.rules)
    else:
        found = discover_actions(e, m.rules)
    for acts in found.values():
        for a in acts:
            if a.full_path == path:
                return a
    return None


def _incoming_action(m: Any, ex: Dict[str, Any]):
    act = _find_action(m, ex["holder_unit"], ex["path"], ex.get("via"))
    if act is None:
        return None
    if ex.get("rider_mode"):
        act = dataclasses.replace(act, container_path="")
    return act


def _bindings(m: Any, ex: Dict[str, Any], writable: bool,
              allow_redirect: bool, notes: List[str]) -> Dict[str, Any]:
    """incoming_of() and redirect() for a half or the body."""
    from action import ActionFail

    def incoming_of(eid: Any) -> IncomingProxy:
        if not isinstance(eid, str):
            raise ActionFail(f"incoming_of() takes a unit id, got {eid!r}.",
                             reason="usage")
        cur = m.reaction_exchange or {}
        if eid not in (cur.get("incoming") or {}):
            raise ActionFail(f"incoming_of(): `{eid}` isn't affected by "
                             f"`{ex['name']}`.", reason="usage")
        return IncomingProxy(m, eid, writable)

    def redirect(old: Any, new: Any) -> bool:
        if not allow_redirect:
            raise ActionFail("redirect() works only in a before half.",
                             reason="usage")
        return _redirect(m, old, new)

    return {"incoming_of": incoming_of, "redirect": redirect}


def _redirect(m: Any, old: Any, new: Any) -> bool:
    from action import ActionFail
    ex = m.reaction_exchange
    ttype = ex["target_type"]
    if ttype not in ("entity", "entity_list"):
        raise ActionFail(f"redirect(): `{ex['name']}` doesn't target units "
                         f"by id, so it can't be redirected.",
                         reason="redirect")
    targets = unit_targets(ttype, ex.get("target"))
    if not isinstance(old, str) or old not in targets:
        raise ActionFail(f"redirect(): {old!r} isn't a target of "
                         f"`{ex['name']}`.", reason="redirect")
    if not isinstance(new, str) or new not in m.entities:
        raise ActionFail(f"redirect(): {new!r} isn't a unit.",
                         reason="redirect")
    if new in targets:
        raise ActionFail(f"redirect(): `{new}` is a target already.",
                         reason="redirect")
    if ttype == "entity":
        ex["target"] = new
    else:
        ex["target"] = [new if t == old else t for t in ex["target"]]
    aff = ex.get("affected") or []
    ex["affected"] = [new if a == old else a for a in aff]
    copies = ex.setdefault("incoming", {})
    if old in copies:
        copies[new] = copies.pop(old)
    ex.setdefault("dropped", []).append(old)
    ex.setdefault("redirects", []).append([old, new])
    return True


def _record_use(m: Any, ex: Dict[str, Any], pick: Dict[str, Any],
                act: Any) -> None:
    m.reaction_log.append({
        "unit": pick["unit"], "path": pick["path"], "name": pick["name"],
        "tags": list(act.tags), "seq": ex["seq"], "action": ex["name"],
        "turn": m.turns_elapsed, "round": int(m.round_number),
    })
    if len(m.reaction_log) > REACTION_LOG_MAX:
        del m.reaction_log[:len(m.reaction_log) - REACTION_LOG_MAX]


async def _run_half(ctx: Any, mgr: Any, m: Any, ex: Dict[str, Any],
                    pick: Dict[str, Any], half: str, pays: bool,
                    out: List[str]) -> Optional[Any]:
    """Run one half of a picked reaction. Returns the match (re-fetched
    by the caller) or None. `out` gets the lines."""
    from action import run_action, ArgsProxy, ActionEngineFault
    uid = pick["unit"]
    act = _find_action(m, uid, pick["path"], pick.get("via"))
    if act is None or uid not in m.entities or \
            not m.entities[uid].is_alive:
        out.append(m.unit_tag(uid) + f"- {_name(m, uid)}'s `{pick['name']}` "
                   f"doesn't run: the reaction or its unit is gone.")
        return None
    spec = act.reaction
    src = spec.before if half == "before" else spec.after
    if not src:
        return None
    eff, mextra, act = mount_routing(m, uid, act, pick.get("via"))
    first = not pick.get("ran")
    half_act = dataclasses.replace(
        act, body=src, prepare="", cost=act.cost if pays else "",
        say_format=act.say_format if first else "")
    own_target = target_from_json(act.target_type, jcopy(pick.get("target")))
    writable = half == "before"
    key = copy_key(m, ex, uid)
    extra_ctx = {
        "actor": ex["eff_actor"],
        "affected": list(ex.get("affected") or []),
        "incoming": IncomingProxy(m, key, writable),
        "action_name": ex["name"],
        "action_tags": list(ex.get("tags") or []),
    }
    if mextra:
        extra_ctx.update(mextra)
    extra_bindings = _bindings(m, ex, writable, half == "before", out)
    extra_bindings.update({
        "target": target_from_json(ex["target_type"],
                                   jcopy(ex.get("target"))),
        "args": ArgsProxy(dict(ex.get("args") or {})),
        "reaction_target": own_target,
        "reaction_args": ArgsProxy(dict(pick.get("args") or {})),
    })
    out.append(m.unit_tag(uid) + (
        f"↪ {_name(m, uid)} reacts: `{pick['name']}`." if first else
        f"↪ {_name(m, uid)}'s `{pick['name']}` follows up."))
    steps = ("cost", "body") if pays else ("body",)
    try:
        with _Runtime(m, ctx, mgr):
            ok, why = await run_action(
                half_act, actor_id=eff, target=own_target,
                args=dict(pick.get("args") or {}), match=m, mgr=mgr,
                ctx=ctx, answers=list(pick.get("answers") or []),
                extra_ctx=extra_ctx, steps=steps, finish=first,
                extra_bindings=extra_bindings)
    except ActionEngineFault as ef:
        out.append(m.unit_tag(uid) + f"❌ {_name(m, uid)}'s "
                   f"`{pick['name']}`: {ef.message}")
        return None
    if not ok:
        reason, message = why if isinstance(why, tuple) else ("", why)
        tag = f"[{reason}] " if reason else ""
        out.append(m.unit_tag(uid) + f"❌ {_name(m, uid)}'s "
                   f"`{pick['name']}` failed: {tag}{message}")
        return None
    return act


def _check_cancel(m: Any, ex: Dict[str, Any], pick: Dict[str, Any]) -> None:
    """After a before half: a copy's `cancel` cancels the action, a unit's
    `avoid` takes that unit out of it."""
    copies = ex.get("incoming") or {}
    if not ex.get("cancelled") and any(
            isinstance(c, dict) and c.get("cancel") for c in copies.values()):
        ex["cancelled"] = True
        ex["cancelled_by"] = [pick["unit"], pick["name"]]
    for key, c in list(copies.items()):
        if key and isinstance(c, dict) and c.get("avoid") \
                and key in (ex.get("affected") or []):
            ex["affected"] = [a for a in ex["affected"] if a != key]
            ex.setdefault("dropped", []).append(key)


def _target_valid(m: Any, act: Any, actor: str, tid: str,
                  extra: Optional[Dict[str, Any]]) -> Optional[str]:
    """Why target `tid` is no longer valid for the action, or None."""
    from action import _target_tag_lines, target_condition_ok
    e = m.entities.get(tid)
    if e is None or not e.is_alive:
        return f"{_name(m, tid)} is gone" if e is None else \
            f"{_name(m, tid)} is dead"
    hits = m.action_restrictions(ACTION_IMMUNE_FIELDS,
                                 m.action_immune_units(tid), act.name,
                                 act.tags, actor, tid)
    if hits:
        return m.action_restriction_line(
            "action_immune_message_format", hits[0], act.name, act.tags,
            actor, tid)
    lines = _target_tag_lines(act, actor, tid, m, False)
    if lines:
        return lines[0]
    ok, err = target_condition_ok(act, actor, tid, m)
    if not ok:
        return (f"{_name(m, tid)} no longer meets `{act.name}`'s target "
                f"condition" + (f" ({err})" if err and not
                                FORMULA_TEXT_HIDDEN.get() else ""))
    return None


def _affects(m: Any, act: Any, actor: str, target: Any,
             args: Dict[str, Any]) -> List[str]:
    """The units the action directly affects: its `affects` expression,
    else its unit targets. VTTError when the expression fails."""
    from formula import FormulaEngine, EvalCtx
    if not act.affects:
        out = unit_targets(act.target_type, target_to_json(
            act.target_type, target))
        return [u for u in out if u in m.entities]
    from action import Coord
    # A location target reads as its cell, [x, y] (or [x, y, map]), which
    # the area functions take whole.
    if isinstance(target, Coord):
        target = target.store_value()
    elif isinstance(target, list):
        target = [t.store_value() if isinstance(t, Coord) else t
                  for t in target]
    try:
        val = FormulaEngine(m).eval_expression(
            act.affects, EvalCtx(this=m.current_entity_id(), target=actor,
                                 extras={"actor": actor, "target": target,
                                         "args": dict(args),
                                         "action_name": act.name,
                                         "action_tags": list(act.tags)}))
    except Exception as err:      # noqa: BLE001
        raise VTTError(f"`{act.name}`'s `affects` "
                       f"{_formula_error(err)}.")
    if val is None:
        return []
    if isinstance(val, str):
        val = [val]
    if not isinstance(val, (list, tuple)):
        raise VTTError(f"`{act.name}`'s `affects` must give unit ids, got "
                       f"{type(val).__name__}.")
    out: List[str] = []
    for v in val:
        if isinstance(v, str) and v in m.entities and v not in out:
            out.append(v)
    return out


def _reread(m: Any, ex: Dict[str, Any], act: Any,
            extra: Optional[Dict[str, Any]], out: List[str]) -> None:
    """Design §6: read again who the action affects and whether each
    target is still valid."""
    actor = ex["eff_actor"]
    ttype = ex["target_type"]
    dropped = ex.setdefault("dropped", [])
    targets = unit_targets(ttype, ex.get("target"))
    keep: List[str] = []
    for t in targets:
        if t in dropped:
            continue
        why = _target_valid(m, act, actor, t, extra)
        if why is not None:
            out.append(m.unit_tag(t) + f"⚠️ {_name(m, t)} drops out of "
                       f"`{ex['name']}`: {why}.")
            dropped.append(t)
            continue
        keep.append(t)
    if ttype == "entity":
        ex["target"] = keep[0] if keep else None
    elif ttype == "entity_list":
        ex["target"] = keep
    if act.affects:
        try:
            now = _affects(m, act, actor, target_from_json(
                ttype, jcopy(ex.get("target"))), ex.get("args") or {})
        except VTTError as err:
            out.append(f"⚠️ {err} The units it affected stay as they were.")
            now = list(ex.get("affected") or [])
    else:
        now = keep
    copies = ex.setdefault("incoming", {})
    affected: List[str] = []
    for u in now:
        if u in dropped:
            continue
        if u not in (ex.get("affected") or []) and u not in copies:
            copies[u] = jcopy(copies.get("", {}))
        affected.append(u)
    for u in ex.get("affected") or []:
        if u not in affected and u not in dropped:
            dropped.append(u)
    ex["affected"] = affected


def _ordered(m: Any, ex: Dict[str, Any], picks: List[Dict[str, Any]],
             half: str) -> List[Dict[str, Any]]:
    tie = str(m.rules.get("reaction_tiebreak", "picked"))
    order = {eid: i for i, eid in enumerate(m.turn_order)}

    def key(p: Dict[str, Any]):
        act = _find_action(m, p["unit"], p["path"], p.get("via"))
        pr = (effective_priority(m, act, half,
                                 continuation=p.get("phase") == "before")
              if act is not None else 0)
        t = order.get(p["unit"], len(order)) if tie == "turn_order" else 0
        return (-pr, t, p["n"])
    return sorted(picks, key=key)


# ----------------------------------------------------------------------
# Begin, resolve, close
# ----------------------------------------------------------------------

def has_holders(m: Any) -> bool:
    return bool(m._uflags.holders_of(m, UnitFlags.REACTIONS))


async def begin(ctx: Any, mgr: Any, m: Any, action: Any, *,
                actor_id: str, eff_actor_id: str, target: Any,
                args: Dict[str, Any], answers: List[Any],
                extra_ctx: Optional[Dict[str, Any]],
                via: Optional[str], rider_mode: bool) -> None:
    """A typed action that may open a reaction window (design §3)."""
    from action import run_action, ActionEngineFault
    pout: Dict[str, Any] = {}
    tout: Dict[str, Any] = {}
    with _Runtime(m, ctx, mgr):
        try:
            ok, why = await run_action(
                action, actor_id=eff_actor_id, target=target, args=args,
                match=m, mgr=mgr, ctx=ctx, answers=answers,
                extra_ctx=extra_ctx, steps=("prepare",), finish=False,
                prepare_out=pout, target_out=tout)
        except ActionEngineFault as ef:
            return await ctx.send(f"❌ action `{action.name}`: {ef.message}")
    if not ok:
        reason, message = why
        tag = f"[{reason}] " if reason else ""
        return await ctx.send(f"❌ action `{action.name}`: {tag}{message}")
    target = tout.get("target", target)
    used = int(tout.get("answers_used", 0))
    bad = [k for k in pout if k in CONTROL_KEYS]
    if bad:
        return await ctx.send(
            f"❌ action `{action.name}`: its `prepare` can't use `{bad[0]}` "
            f"as a local: it is a key of `incoming` the reactions set.")
    base, dropped = _storable_locals(pout)
    notes: List[str] = []
    if dropped:
        notes.append(f"⚠️ `{action.name}`'s `prepare` holds "
                     + ", ".join(f"`{d}`" for d in dropped)
                     + ", which the reaction window can't keep (not a "
                       "value a var could store): the body won't see "
                       + ("it." if len(dropped) == 1 else "them."))
    try:
        affected = _affects(m, action, eff_actor_id, target, args)
    except VTTError as err:
        return await ctx.send(f"❌ action `{action.name}`: {err}")
    m.reaction_seq += 1
    ex: Dict[str, Any] = {
        "seq": m.reaction_seq, "phase": "before",
        "actor": actor_id, "eff_actor": eff_actor_id,
        "holder_unit": actor_id, "path": action.full_path, "via": via,
        "rider_mode": rider_mode, "mount": jcopy(extra_ctx),
        "name": action.name, "tags": list(action.tags),
        "target_type": action.target_type,
        "target": target_to_json(action.target_type, target),
        "args": _own_value(dict(args)), "answers": list(answers[used:]),
        "affected": list(affected), "first_affected": list(affected),
        "dropped": [], "incoming": {"": base},
        "picks": [], "pick_seq": 0, "channel": str(getattr(
            ctx, "channel_key", "")),
    }
    for u in affected:
        ex["incoming"][u] = jcopy(base)
    m.reaction_exchange = ex
    lines = open_window(m, ex, "before", notes)
    _refresh_done(m, ex)
    if not waiting(ex):
        if notes or lines:
            await ctx.send("\n".join(notes + lines))
        return await resolve(ctx, mgr, m)
    m._reaction_opened = time.time()
    head = (f"⏳ {_action_line(m, ex)}: reactions are offered before it "
            f"resolves.")
    await ctx.send("\n".join(notes + [head] + lines + [
        "A host closes the window with `!react skip`."]))


async def resolve(ctx: Any, mgr: Any, m: Any) -> None:
    """The open window closed: run what it decided (design §3 steps 6-9)."""
    ex = m.reaction_exchange
    if ex is None:
        return
    m._reaction_opened = None
    if ex.get("phase") == "after":
        return await _finish_after(ctx, mgr, m)
    out: List[str] = []
    act = _incoming_action(m, ex)
    if act is None or ex["eff_actor"] not in m.entities:
        m.reaction_exchange = None
        return await ctx.send(f"⚠️ {_name(m, ex['actor'])}'s `{ex['name']}` "
                              f"doesn't resolve: the action or its unit is "
                              f"gone. Nothing runs.")
    start = m.to_dict(include_history=False)
    hist_mark = m.history.mark()
    befores = [p for p in ex.get("picks") or [] if p.get("phase") == "before"]
    for pick in _ordered(m, ex, befores, "before"):
        m = mgr.matches.get(m.id, m)
        ex = m.reaction_exchange
        cur = next((p for p in ex["picks"] if p["n"] == pick["n"]), None)
        if cur is None:
            continue
        r_act = _find_action(m, cur["unit"], cur["path"], cur.get("via"))
        if r_act is None or not m.entities[cur["unit"]].is_alive:
            cur["skipped"] = True
            out.append(m.unit_tag(cur["unit"]) + f"- {_name(m, cur['unit'])}"
                       f"'s `{cur['name']}` doesn't run: the reaction or "
                       f"its unit is gone.")
            continue
        if ex.get("cancelled"):
            mode = r_act.reaction.on_cancel or str(
                m.rules.get("reaction_on_cancel", "refund"))
            if mode == "refund":
                cur["refunded"] = True
                out.append(m.unit_tag(cur["unit"]) + f"↩ {_name(m, cur['unit'])}"
                           f"'s `{cur['name']}` is refunded: the action was "
                           f"cancelled.")
                continue
        notes: List[str] = []
        if not _reaction_open(m, ex, cur["unit"], r_act, "before", notes):
            out += notes
            cur["skipped"] = True
            out.append(m.unit_tag(cur["unit"]) + f"- {_name(m, cur['unit'])}"
                       f"'s `{cur['name']}` is no longer available; it "
                       f"doesn't run.")
            continue
        await _flush(ctx, out)
        ran = await _run_half(ctx, mgr, m, ex, cur, "before", True, out)
        m = mgr.matches.get(m.id, m)
        ex = m.reaction_exchange
        cur = next((p for p in ex["picks"] if p["n"] == pick["n"]), cur)
        if ran is not None:
            cur["ran"] = True
            _record_use(m, ex, cur, ran)
            _check_cancel(m, ex, cur)
    m = mgr.matches.get(m.id, m)
    ex = m.reaction_exchange
    act = _incoming_action(m, ex)
    if act is None or ex["eff_actor"] not in m.entities:
        ex["outcome"] = "cancelled"
        out.append(f"⚠️ {_name(m, ex['actor'])}'s `{ex['name']}` doesn't "
                   f"resolve: the action or its unit is gone.")
        await _flush(ctx, out)
        return await _open_after(ctx, mgr, m)
    extra = ex.get("mount") or None
    if ex.get("cancelled"):
        await _cancelled(ctx, mgr, m, act, out)
        m = mgr.matches.get(m.id, m)
        return await _open_after(ctx, mgr, m)
    _reread(m, ex, act, extra, out)
    await _flush(ctx, out)
    await _run_body(ctx, mgr, m, act, start, hist_mark, out)
    m = mgr.matches.get(m.id, m)
    await _flush(ctx, out)
    await _open_after(ctx, mgr, m)


async def _flush(ctx: Any, out: List[str]) -> None:
    if out:
        await ctx.send("\n".join(out))
        out.clear()


async def _cancelled(ctx: Any, mgr: Any, m: Any, act: Any,
                     out: List[str]) -> None:
    from action import run_action, ActionEngineFault
    ex = m.reaction_exchange
    ex["outcome"] = ex["ended"] = "cancelled"
    by = ex.get("cancelled_by") or ["", ""]
    who = f"{_name(m, by[0])}'s `{by[1]}`" if by[0] else "a reaction"
    out.append(f"🚫 {_name(m, ex['actor'])}'s `{ex['name']}` is cancelled "
               f"by {who}.")
    pay = act.cancelled_action_cost or str(
        m.rules.get("cancelled_action_cost", "refund"))
    if pay == "pay" and act.cost:
        await _flush(ctx, out)
        try:
            with _Runtime(m, ctx, mgr):
                ok, why = await run_action(
                    act, actor_id=ex["eff_actor"],
                    target=target_from_json(ex["target_type"],
                                            jcopy(ex.get("target"))),
                    args=dict(ex.get("args") or {}), match=m, mgr=mgr,
                    ctx=ctx, extra_ctx=ex.get("mount") or None,
                    steps=("cost",), finish=False, skip_checks=True,
                    preset_locals=_plain((ex.get("incoming") or {}).get(
                        "", {})))
            m = mgr.matches.get(m.id, m)
            if not ok:
                reason, message = why
                out.append(f"⚠️ `{ex['name']}`'s cost failed: {message}")
        except ActionEngineFault as ef:
            out.append(f"⚠️ `{ex['name']}`'s cost: {ef.message}")
    m.fire_action_failed(
        action_tags=act.tags, actor_id=ex["eff_actor"],
        action_name=act.name, action_path=act.full_path,
        target=target_from_json(ex["target_type"], jcopy(ex.get("target"))),
        args=dict(ex.get("args") or {}), fail_reason="cancelled",
        fail_message=f"cancelled by {by[0] or 'a reaction'}")
    m.log_event("action_failed", actor=ex["eff_actor"],
                actor_name=m._entity_name(ex["eff_actor"]),
                action=act.name, action_path=act.full_path,
                target=str(ex.get("target")), reason="cancelled",
                message=f"cancelled by {by[0]}")
    await _flush(ctx, out)


async def _run_body(ctx: Any, mgr: Any, m: Any, act: Any,
                    start: Dict[str, Any], hist_mark: Any,
                    out: List[str]) -> None:
    from action import run_action, ActionEngineFault, _rollback_match
    ex = m.reaction_exchange
    affected = list(ex.get("affected") or [])
    copies = ex.get("incoming") or {}
    key = affected[0] if len(affected) == 1 else ""
    preset = _plain(copies.get(key, copies.get("", {})))
    extra_ctx = dict(ex.get("mount") or {})
    extra_ctx.update({"affected": affected,
                      "incoming": IncomingProxy(m, key, True)})
    bindings = _bindings(m, ex, True, False, out)
    bout: Dict[str, Any] = {}
    fout: Dict[str, Any] = {}
    target = target_from_json(ex["target_type"], jcopy(ex.get("target")))
    try:
        with _Runtime(m, ctx, mgr):
            ok, why = await run_action(
                act, actor_id=ex["eff_actor"], target=target,
                args=dict(ex.get("args") or {}), match=m, mgr=mgr, ctx=ctx,
                answers=list(ex.get("answers") or []), extra_ctx=extra_ctx,
                steps=("cost", "body"), preset_locals=preset,
                skip_checks=True, extra_bindings=bindings, body_out=bout,
                fail_out=fout)
    except ActionEngineFault as ef:
        ok, why = False, ("", ef.message)
    m = mgr.matches.get(m.id, m)
    ex = m.reaction_exchange
    if not ok:
        reason, message = why if isinstance(why, tuple) else ("", why)
        tag = f"[{reason}] " if reason else ""
        if fout.get("refund"):
            # fail(refund): the whole exchange goes back to before the
            # reactions ran; nothing is recorded as used.
            keep_ex = jcopy(ex)
            _rollback_match(m, mgr, start)
            m.history.rewind(hist_mark)
            m = mgr.matches.get(m.id, m)
            m.reaction_exchange = keep_ex
            ex = keep_ex
            for p in ex.get("picks") or []:
                p["refunded"] = True
            ex["refunded"] = True
            out.append(f"❌ action `{act.name}`: {tag}{message} (refunded: "
                       f"the reactions are undone too)")
        else:
            out.append(f"❌ action `{act.name}`: {tag}{message}")
        ex["outcome"] = "cancelled"
        return
    ex["outcome"] = ex["ended"] = "resolved"
    body_vals, _dropped = _storable_locals(bout)
    for k in list(ex.get("incoming") or {}):
        c = ex["incoming"][k]
        for name, v in body_vals.items():
            if name not in CONTROL_KEYS:
                c[name] = jcopy(v)
    out.append(f"`{ex['actor']}` used action `{act.name}`.")


async def _open_after(ctx: Any, mgr: Any, m: Any) -> None:
    ex = m.reaction_exchange
    if ex is None:
        return
    notes: List[str] = []
    if ex.get("refunded"):
        lines: List[str] = []
        ex["phase"] = "after"
        ex["units"], ex["unit_order"] = {}, []
    else:
        lines = open_window(m, ex, "after", notes)
        _refresh_done(m, ex)
    if waiting(ex):
        m._reaction_opened = time.time()
        await ctx.send("\n".join(notes + [
            f"⏳ {_action_line(m, ex)} "
            + {"resolved": "has resolved", "cancelled": "was cancelled"}.get(
                ex.get("ended", ""), "failed")
            + ": reactions to it are offered."] + lines + [
            "A host closes the window with `!react skip`."]))
        return
    if notes or lines:
        await ctx.send("\n".join(notes + lines))
    await _finish_after(ctx, mgr, m)


async def _finish_after(ctx: Any, mgr: Any, m: Any) -> None:
    """Every after half in one order: the continuations of before picks
    (their after_on permitting) and the after-only picks."""
    ex = m.reaction_exchange
    m._reaction_opened = None
    out: List[str] = []
    runs: List[Dict[str, Any]] = []
    for p in ex.get("picks") or []:
        if p.get("refunded") or p.get("skipped"):
            continue
        if p.get("phase") == "before":
            if not p.get("ran"):
                continue
            act = _find_action(m, p["unit"], p["path"], p.get("via"))
            if act is None or not act.reaction.after:
                continue
            want = act.reaction.after_on
            if want != "any" and want != unit_outcome(ex, p["unit"], m):
                continue
        runs.append(p)
    for pick in _ordered(m, ex, runs, "after"):
        m = mgr.matches.get(m.id, m)
        ex = m.reaction_exchange
        cur = next((p for p in ex["picks"] if p["n"] == pick["n"]), None)
        if cur is None:
            continue
        if cur.get("phase") == "after":
            r_act = _find_action(m, cur["unit"], cur["path"], cur.get("via"))
            notes: List[str] = []
            if r_act is None or not _reaction_open(
                    m, ex, cur["unit"], r_act, "after", notes):
                out += notes
                out.append(m.unit_tag(cur["unit"]) + f"- "
                           f"{_name(m, cur['unit'])}'s `{cur['name']}` is no "
                           f"longer available; it doesn't run.")
                continue
        await _flush(ctx, out)
        pays = cur.get("phase") == "after"
        ran = await _run_half(ctx, mgr, m, ex, cur, "after", pays, out)
        m = mgr.matches.get(m.id, m)
        ex = m.reaction_exchange
        cur = next((p for p in ex["picks"] if p["n"] == pick["n"]), cur)
        if ran is not None and not cur.get("ran"):
            cur["ran"] = True
            _record_use(m, ex, cur, ran)
    m = mgr.matches.get(m.id, m)
    m.reaction_exchange = None
    m._reaction_opened = None
    await _flush(ctx, out)


# ----------------------------------------------------------------------
# !react
# ----------------------------------------------------------------------

async def react_pick(ctx: Any, mgr: Any, m: Any, uid: str, requested: str,
                     tail: List[str], *, missing=None, missing_corpse=None,
                     map_of=None) -> None:
    from action import (lookup_action, parse_target, parse_args_tokens,
                        dedupe_targets)
    ex = m.reaction_exchange
    st = (ex.get("units") or {}).get(uid)
    if st is None:
        raise VTTError(f"`{uid}` has no reaction to pick in this window.")
    if st.get("done"):
        raise VTTError(f"`{uid}` is done answering this window.")
    phase = ex["phase"]
    opts = available(m, ex, uid, phase)
    by_name: Dict[str, List[Any]] = {}
    match = None
    for a, via in opts:
        by_name.setdefault(a.name, []).append((a, via))
        if a.full_path == requested:
            match = (a, via)
    if match is None:
        found = lookup_action({k: [a for a, _v in v]
                               for k, v in by_name.items()},
                              requested, m.rules)
        if not found:
            names = ", ".join(f"`{n}`" for n in by_name) or "none"
            raise VTTError(f"`{uid}` can't pick `{requested}` now. It can "
                           f"pick: {names}.")
        if len(found) > 1:
            raise VTTError(f"`{uid}` has {len(found)} reactions named "
                           f"`{requested}`: name one by its path ("
                           + ", ".join(f"`{a.full_path}`" for a in found)
                           + ").")
        match = next((a, v) for a, v in opts if a is found[0])
    act, via = match
    answers = [t.split("=", 1)[1] for t in tail if t.startswith("answer=")]
    rest = [t for t in tail if not t.startswith("answer=")]
    target, remaining = parse_target(act.target_type, rest, m, uid,
                                     missing=missing,
                                     missing_corpse=missing_corpse,
                                     map_of=map_of)
    args = parse_args_tokens(remaining)
    target, dup = dedupe_targets(act.target_type, target, act.name)
    _pick(m, ex, uid, act, via, phase, target=target, args=args,
          answers=answers)
    _refresh_done(m, ex)
    lines = ([dup] if dup else []) + [
        m.unit_tag(uid) + f"✋ {_name(m, uid)} picks `{act.name}`."]
    if not st.get("done"):
        more = available(m, ex, uid, phase)
        lines.append(m.unit_tag(uid) + "It can add: " + ", ".join(
            f"`!react {uid} {a.name}`" for a, _v in more)
            + f" — or `!react {uid} pass`.")
    await ctx.send("\n".join(lines))
    await maybe_close(ctx, mgr, m)


async def react_pass(ctx: Any, mgr: Any, m: Any, uid: str) -> None:
    ex = m.reaction_exchange
    st = (ex.get("units") or {}).get(uid)
    if st is None:
        raise VTTError(f"`{uid}` has nothing to answer in this window.")
    if st.get("done"):
        raise VTTError(f"`{uid}` is done answering this window.")
    st["done"] = True
    await ctx.send(m.unit_tag(uid) + f"🤚 {_name(m, uid)} passes.")
    await maybe_close(ctx, mgr, m)


async def react_skip(ctx: Any, mgr: Any, m: Any, why: str = "") -> None:
    ex = m.reaction_exchange
    for uid in waiting(ex):
        ex["units"][uid]["done"] = True
    await ctx.send(why or "⏭ The host closes the reaction window.")
    await resolve(ctx, mgr, m)


async def maybe_close(ctx: Any, mgr: Any, m: Any) -> None:
    m = mgr.matches.get(m.id, m)
    ex = m.reaction_exchange
    if ex is None:
        return
    _refresh_done(m, ex)
    if not waiting(ex):
        await resolve(ctx, mgr, m)


def expired(m: Any) -> bool:
    """Whether the open window has outlived reaction_window_expiry_seconds
    (counted from when it opened in this process; a window loaded from
    disk starts counting now)."""
    if m.reaction_exchange is None:
        return False
    try:
        limit = int(m.rules.get("reaction_window_expiry_seconds", -1))
    except (TypeError, ValueError):
        return False
    if limit < 0:
        return False
    if m._reaction_opened is None:
        m._reaction_opened = time.time()
        return False
    return time.time() - m._reaction_opened >= limit


async def settle_expiry(ctx: Any, mgr: Any, m: Any) -> None:
    """Close an expired window as skipped (the command running now resolves
    it; journaled as the state it leaves, since a replay can't tell
    time)."""
    if not expired(m):
        return
    js = JOURNAL_AS_STATE.get()
    if js is not None:
        js[0] = True
    await react_skip(ctx, mgr, m, "⌛ The reaction window ran out "
                     "(reaction_window_expiry_seconds); it closes as "
                     "skipped.")


# ----------------------------------------------------------------------
# Actions a formula starts, reactions_used
# ----------------------------------------------------------------------

def unoffered_note(m: Any, act: Any, actor: str, target: Any) -> Optional[str]:
    """A note for an action a formula started (no window of its own) when
    a unit holds a reaction that would answer it (reacts_to, reacts_on;
    conditions aren't read)."""
    if not has_holders(m):
        return None
    affected = unit_targets(act.target_type, target_to_json(
        act.target_type, target))
    fake = {"actor": actor, "eff_actor": actor, "tags": list(act.tags),
            "affected": affected, "incoming": {}}
    for uid in _candidates(m):
        for a, _via in unit_reactions(m, m.entities[uid]):
            spec = a.reaction
            if spec.disabled or not (spec.before or spec.after):
                continue
            if uid == actor and not spec.self_trigger:
                continue
            try:
                if spec.reacts_to is not None and not tag_filter_matches(
                        list(act.tags), spec.reacts_to):
                    continue
            except VTTError:
                continue
            if spec.reacts_on == "target" and not _covered(
                    m, fake, uid, affected):
                continue
            return (f"ℹ️ `{act.name}` ran from a formula, so reactions to it "
                    f"weren't offered.")
    return None


def reactions_used(m: Any, eid: str, since: str = "own_turn",
                   tag: Any = None) -> int:
    if since not in SINCE_WORDS:
        raise VTTError(f"reactions_used(since=): one of "
                       f"{', '.join(SINCE_WORDS)}, not {since!r}.")
    if since == "round" and bool(m.rules.get("atb_enabled", False)):
        raise VTTError("reactions_used(since='round'): ATB has no rounds; "
                       "use 'turn', 'own_turn' or 'ever'.")
    if since == "action" and m.reaction_exchange is None \
            and not m.reaction_seq:
        return 0
    n = 0
    start = m.own_turn_at.get(eid, 0)
    for r in m.reaction_log:
        if r.get("unit") != eid:
            continue
        if since == "own_turn" and int(r.get("turn", 0)) < start:
            continue
        if since == "round" and int(r.get("round", 0)) != int(m.round_number):
            continue
        if since == "turn" and int(r.get("turn", 0)) != m.turns_elapsed:
            continue
        if since == "action" and int(r.get("seq", 0)) != m.reaction_seq:
            continue
        if tag is not None:
            try:
                if not tag_filter_matches(r.get("tags") or [], tag):
                    continue
            except VTTError as err:
                raise VTTError(f"reactions_used(tag=): {err}")
        n += 1
    return n
