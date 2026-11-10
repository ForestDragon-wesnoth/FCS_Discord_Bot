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

Exchanges live on `Match.reaction_stack` (JSON, saved, undo state); the
top one's window is open. An action a formula starts inside a resolving
step (a body, a cost, a reaction's half) whose units may react opens an
automatic react point: the step rolls back, the window of the action it
started opens on top of the stack, and once that window closes the step
runs again from its start, the action resolving at that point with the
picks made (`_run_step`, `_point_use`). An action a formula starts outside
any action (a passive, a watcher) opens its window at once and the formula
carries on (use_action returns None), or waits in `Match.reaction_queue`
while another window is open (`_queue_use`). The wall clock of the open
window is runtime (`Match._reaction_opened`).

Not built yet (design §13): reacting to a reaction's half itself, a
`react_point` the GM places, Discord buttons.
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
    action's locals in exchange `seq`, read through the match each time (a
    reaction half that fails rolls the match back, exchange included). A
    proxy made for a reacting unit (`uid`) finds that unit's copy on every
    read, so a bystander that redirects the action onto itself reads the
    copy it took over. A key nobody set reads None. Writable in before
    halves and the body; read-only in conditions and after halves."""

    __slots__ = ("_m", "_seq", "_key", "_uid", "_writable")

    def __init__(self, match: Any, seq: int, key: str = "",
                 writable: bool = False, uid: Optional[str] = None) -> None:
        object.__setattr__(self, "_m", match)
        object.__setattr__(self, "_seq", seq)
        object.__setattr__(self, "_key", key)
        object.__setattr__(self, "_uid", uid)
        object.__setattr__(self, "_writable", writable)

    def _exchange(self) -> Dict[str, Any]:
        ex = _ex(self._m, self._seq)
        if ex is None:
            raise AttributeError("that action has finished resolving")
        return ex

    def _copy(self) -> Dict[str, Any]:
        ex = self._exchange()
        key = self._key if self._uid is None else copy_key(self._m, ex,
                                                            self._uid)
        return ex.setdefault("incoming", {}).setdefault(key, {})

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        ex = self._exchange()
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
        return f"incoming({self._uid or self._key or 'action'})"


def _ex(m: Any, seq: Any) -> Optional[Dict[str, Any]]:
    """Exchange `seq` on the match's reaction stack, or None."""
    for ex in reversed(m.reaction_stack):
        if ex.get("seq") == seq:
            return ex
    return None


def _drop(m: Any, seq: Any) -> None:
    m.reaction_stack[:] = [x for x in m.reaction_stack
                           if x.get("seq") != seq]


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
    body part of it the action affects (reactions_cover_parts), else (a
    bystander) the copy of the action's one affected unit when it affects
    exactly one (user call: a bystander halving a single-target attack
    reaches the body), else the action's shared copy."""
    copies = ex.get("incoming") or {}
    if uid in copies:
        return uid
    if m.rules.get("reactions_cover_parts", True):
        for key in copies:
            if key and uid in _ancestors(m, key):
                return key
    affected = [a for a in ex.get("affected") or [] if a in copies]
    if len(affected) == 1:
        return affected[0]
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
        "incoming": IncomingProxy(m, ex["seq"], writable=writable, uid=uid),
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
    stored = ((ex.get("units") or {}).get(uid) or {}).get("options")
    if ex.get("point") and stored is not None:
        # A react point's window: the board is back where the paused step
        # began, so the options it opened with stand; conditions are read
        # again when the action resolves at that point.
        out = []
        for path, via in stored:
            a = _find_action(m, uid, path, via)
            if a is not None and a.full_path not in picked:
                out.append((a, via))
        return out
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
    has none), highest priority first, up to the per-window limit. A
    reaction taking one unit as its own target gets the incoming action's
    actor (user call); one taking any other target isn't picked."""
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
        own_target = None
        if act.target_type == "entity":
            own_target = ex["eff_actor"]
            if own_target not in m.entities:
                continue
        elif act.target_type != "none":
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
        _pick(m, ex, uid, act, via, phase, target=own_target, auto=True)
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
        ex["units"][uid] = {"done": False, "picked": 0,
                            "options": [[a.full_path, v] for a, v in opts]}
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
    pt = ex.get("point")
    by = (f" (started by `{pt['by']}`)" if isinstance(pt, dict)
          and pt.get("by") else "")
    lines = [f"⏳ Reactions to {_action_line(m, ex)}{by} ("
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


def _bindings(m: Any, seq: int, writable: bool,
              allow_redirect: bool) -> Dict[str, Any]:
    """incoming_of() and redirect() for a half or the body of exchange
    `seq`."""
    from action import ActionFail

    def incoming_of(eid: Any) -> IncomingProxy:
        if not isinstance(eid, str):
            raise ActionFail(f"incoming_of() takes a unit id, got {eid!r}.",
                             reason="usage")
        cur = _ex(m, seq) or {}
        if eid not in (cur.get("incoming") or {}):
            raise ActionFail(f"incoming_of(): `{eid}` isn't affected by "
                             f"`{cur.get('name', '?')}`.", reason="usage")
        return IncomingProxy(m, seq, eid, writable)

    def redirect(old: Any, new: Any) -> bool:
        if not allow_redirect:
            raise ActionFail("redirect() works only in a before half.",
                             reason="usage")
        return _redirect(m, seq, old, new)

    return {"incoming_of": incoming_of, "redirect": redirect}


def _redirect(m: Any, seq: int, old: Any, new: Any) -> bool:
    from action import ActionFail
    ex = _ex(m, seq)
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
# The engine: a resumable walk through one exchange
# ----------------------------------------------------------------------

def has_holders(m: Any) -> bool:
    return bool(m._uflags.holders_of(m, UnitFlags.REACTIONS))


class _Env:
    """One drive of the engine. `inline`: resolving an action at a react
    point inside a step (no windows of its own: one that must open raises
    ReactPointNeeded; `rec` holds the picks its windows got)."""

    def __init__(self, ctx: Any, mgr: Any, m: Any, inline: bool = False,
                 rec: Optional[Dict[str, Any]] = None) -> None:
        self.ctx, self.mgr, self._m, self.mid = ctx, mgr, m, m.id
        self.inline = inline
        self.rec = rec or {}
        self.out: List[str] = []
        self.window: List[str] = []
        self.start: Dict[int, Tuple[Dict[str, Any], Any]] = {}
        self.finished: Dict[int, str] = {}

    @property
    def m(self) -> Any:
        if self.mgr is not None:
            return self.mgr.matches.get(self.mid, self._m)
        return self._m

    async def flush(self) -> None:
        if not self.out:
            return
        lines, self.out = self.out, []
        if self.inline:
            self.m.surface_log(lines)
        else:
            await self.ctx.send("\n".join(lines))

    def runtime(self):
        return _NoRuntime() if self.inline else _Runtime(self.m, self.ctx,
                                                         self.mgr)


class _NoRuntime:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return None


async def _run_step(env: _Env, seq: int, key: str, run,
                    by: str = "") -> Tuple[str, Any]:
    """Run one step of exchange `seq` (a half, the body, a cost) through
    `run`. In a top drive the step can pause: an action its formulas start
    raises ReactPointNeeded, the match goes back to the step's start, the
    window of that action opens on top of the stack and the exchange
    records where it paused; resumed, the step runs again from its start
    with the RNG where it began and the picks each closed window got
    (`records`, by the use_action count). ("suspended", None) or ("ok",
    run's value)."""
    if env.inline:
        return "ok", await run()
    from action import ReactPointNeeded, REACT_STEP, _rollback_match
    m = env.m
    ex = _ex(m, seq)
    sus = ex.get("suspended") if isinstance(ex.get("suspended"), dict) \
        and ex["suspended"].get("key") == key else None
    records = jcopy(sus.get("records") or {}) if sus else {}
    pos_live = m.rng_position()
    rng0 = int(sus["rng"]) if sus else pos_live
    if sus:
        m.set_rng(pos=rng0)
    pre = m.to_dict(include_history=False)
    mark = m.history.mark()
    step = {"seq": seq, "key": key, "records": records, "counter": [0],
            "by": by or ex.get("name", "")}
    token = REACT_STEP.set(step)
    try:
        value = await run()
    except ReactPointNeeded as rp:
        REACT_STEP.reset(token)
        token = None
        m = env.m
        _rollback_match(m, env.mgr, pre)
        m.history.rewind(mark)
        m = env.m
        child = rp.exchange
        m.reaction_seq = max(m.reaction_seq, int(child.get("seq", 0)))
        ex = _ex(m, seq)
        ex["suspended"] = {"key": key, "rng": rng0, "records": records,
                           "child": child.get("seq")}
        m.reaction_stack.append(child)
        if m.rng_position() < pos_live:
            m.set_rng(pos=pos_live)
        env.window = list(rp.lines)
        return "suspended", None
    finally:
        if token is not None:
            REACT_STEP.reset(token)
    m = env.m
    ex = _ex(m, seq)
    if ex is not None and isinstance(ex.get("suspended"), dict) \
            and ex["suspended"].get("key") == key:
        del ex["suspended"]
    if m.rng_position() < pos_live:
        m.set_rng(pos=pos_live)
    return "ok", value


async def _half(env: _Env, seq: int, n: int, half: str, pays: bool) -> str:
    """Run one half of pick `n`: "ok", "fail", "gone" or "suspended"."""
    from action import (run_action, ArgsProxy, ActionEngineFault,
                        REACT_ORIGIN)
    m = env.m
    ex = _ex(m, seq)
    pick = next(p for p in ex["picks"] if p["n"] == n)
    uid = pick["unit"]
    act = _find_action(m, uid, pick["path"], pick.get("via"))
    if act is None or uid not in m.entities or \
            not m.entities[uid].is_alive:
        env.out.append(m.unit_tag(uid) + f"- {_name(m, uid)}'s "
                       f"`{pick['name']}` doesn't run: the reaction or its "
                       f"unit is gone.")
        return "gone"
    spec = act.reaction
    src = spec.before if half == "before" else spec.after
    if not src:
        return "gone"
    eff, mextra, act = mount_routing(m, uid, act, pick.get("via"))
    first = not pick.get("ran")
    half_act = dataclasses.replace(
        act, body=src, prepare="", cost=act.cost if pays else "",
        say_format=act.say_format if first else "")
    own_target = target_from_json(act.target_type, jcopy(pick.get("target")))
    writable = half == "before"
    extra_ctx = {
        "actor": ex["eff_actor"],
        "affected": list(ex.get("affected") or []),
        "incoming": IncomingProxy(m, seq, writable=writable, uid=uid),
        "action_name": ex["name"],
        "action_tags": list(ex.get("tags") or []),
    }
    if mextra:
        extra_ctx.update(mextra)
    extra_bindings = _bindings(m, seq, writable, half == "before")
    extra_bindings.update({
        "target": target_from_json(ex["target_type"],
                                   jcopy(ex.get("target"))),
        "args": ArgsProxy(dict(ex.get("args") or {})),
        "reaction_target": own_target,
        "reaction_args": ArgsProxy(dict(pick.get("args") or {})),
    })
    sus = ex.get("suspended")
    if not (isinstance(sus, dict) and sus.get("key") == f"half:{n}:{half}"):
        # (A half running again after a react point said this already.)
        env.out.append(m.unit_tag(uid) + (
            f"↪ {_name(m, uid)} reacts: `{pick['name']}`." if first else
            f"↪ {_name(m, uid)}'s `{pick['name']}` follows up."))
    await env.flush()
    steps = ("cost", "body") if pays else ("body",)
    # The actions this half starts sit one reaction deeper (#36 §8).
    origin = (int(ex.get("depth", 0)) + 1, bool(spec.reactable))

    async def run():
        otoken = REACT_ORIGIN.set(origin)
        try:
            with env.runtime():
                return await run_action(
                    half_act, actor_id=eff, target=own_target,
                    args=dict(pick.get("args") or {}), match=env.m,
                    mgr=env.mgr, ctx=env.ctx,
                    answers=list(pick.get("answers") or []),
                    extra_ctx=extra_ctx, steps=steps, finish=first,
                    extra_bindings=extra_bindings)
        except ActionEngineFault as ef:
            return False, ("", ef.message)
        finally:
            REACT_ORIGIN.reset(otoken)
    st, res = await _run_step(env, seq, f"half:{n}:{half}", run,
                              by=pick["name"])
    if st == "suspended":
        return "suspended"
    ok, why = res
    m = env.m
    if not ok:
        reason, message = why if isinstance(why, tuple) else ("", why)
        tag = f"[{reason}] " if reason else ""
        env.out.append(m.unit_tag(uid) + f"❌ {_name(m, uid)}'s "
                       f"`{pick['name']}` failed: {tag}{message}")
        return "fail"
    ex = _ex(m, seq)
    cur = next(p for p in ex["picks"] if p["n"] == n)
    if not cur.get("ran"):
        cur["ran"] = True
        _record_use(m, ex, cur, act)
    return "ok"


def _head(m: Any, ex: Dict[str, Any]) -> str:
    """The first line of an open window."""
    by = ""
    pt = ex.get("point")
    if isinstance(pt, dict) and pt.get("by"):
        by = f" (started by `{pt['by']}`)"
    if ex.get("phase") == "after":
        how = {"resolved": "has resolved", "cancelled": "was cancelled"}.get(
            ex.get("ended", ""), "failed")
        return (f"⏳ {_action_line(m, ex)}{by} {how}: reactions to it are "
                f"offered.")
    return (f"⏳ {_action_line(m, ex)}{by}: reactions are offered before it "
            f"resolves.")


_FOOT = "A host closes the window with `!react skip`."


def _apply_recorded(ex: Dict[str, Any], picks: List[Dict[str, Any]],
                    phase: str) -> None:
    """A react point's window already closed: its picks, as made."""
    keep = [p for p in ex.get("picks") or [] if p.get("phase") != phase]
    seq_n = max([p["n"] for p in keep] + [0])
    for p in picks:
        seq_n += 1
        q = {k: jcopy(v) for k, v in p.items()
             if k not in ("ran", "refunded", "skipped", "failed")}
        q["n"] = seq_n
        keep.append(q)
    ex["picks"] = keep
    ex["pick_seq"] = seq_n
    ex["units"], ex["unit_order"] = {}, []


def _open(env: _Env, seq: int, phase: str) -> bool:
    """Open exchange `seq`'s `phase` window (or take a react point's
    recorded picks). True when units are left to answer; in an inline drive
    that raises ReactPointNeeded instead."""
    from action import ReactPointNeeded
    m = env.m
    ex = _ex(m, seq)
    if env.inline and env.rec.get(phase) is not None:
        _apply_recorded(ex, env.rec[phase], phase)
        ex["phase"] = phase
        return False
    notes: List[str] = []
    lines = open_window(m, ex, phase, notes)
    _refresh_done(m, ex)
    if not waiting(ex):
        env.out += notes + lines
        return False
    window = notes + [_head(m, ex)] + lines + [_FOOT]
    if env.inline:
        raise ReactPointNeeded(jcopy(ex), window)
    env.window = window
    return True


async def _advance(env: _Env, seq: int) -> str:
    """Take exchange `seq` as far as it goes: "window" (a window of its own
    or of an action one of its steps started is open) or "done"."""
    m = env.m
    ex = _ex(m, seq)
    if ex["phase"] == "before":
        if waiting(ex):
            return "window"
        ex["phase"] = "before_run"
    if ex["phase"] == "before_run":
        m = env.m
        ex = _ex(m, seq)
        if seq not in env.start:
            env.start[seq] = (m.to_dict(include_history=False),
                              m.history.mark())
        if _incoming_action(m, ex) is None or ex["eff_actor"] not in \
                m.entities:
            ex["outcome"], ex["phase"], ex["gone"] = "cancelled", "done", True
            env.out.append(f"⚠️ {_name(m, ex['actor'])}'s `{ex['name']}` "
                           f"doesn't resolve: the action or its unit is "
                           f"gone. Nothing runs.")
            env.finished[seq] = "cancelled"
            return "done"
        befores = [p for p in ex.get("picks") or []
                   if p.get("phase") == "before"]
        for pick in _ordered(m, ex, befores, "before"):
            m = env.m
            ex = _ex(m, seq)
            cur = next((p for p in ex["picks"] if p["n"] == pick["n"]), None)
            if cur is None or any(cur.get(k) for k in
                                  ("ran", "refunded", "skipped", "failed")):
                continue
            r_act = _find_action(m, cur["unit"], cur["path"], cur.get("via"))
            if r_act is None or cur["unit"] not in m.entities or \
                    not m.entities[cur["unit"]].is_alive:
                cur["skipped"] = True
                env.out.append(m.unit_tag(cur["unit"]) + f"- "
                               f"{_name(m, cur['unit'])}'s `{cur['name']}` "
                               f"doesn't run: the reaction or its unit is "
                               f"gone.")
                continue
            if ex.get("cancelled"):
                mode = r_act.reaction.on_cancel or str(
                    m.rules.get("reaction_on_cancel", "refund"))
                if mode == "refund":
                    cur["refunded"] = True
                    env.out.append(m.unit_tag(cur["unit"]) + f"↩ "
                                   f"{_name(m, cur['unit'])}'s "
                                   f"`{cur['name']}` is refunded: the "
                                   f"action was cancelled.")
                    continue
            notes: List[str] = []
            if not _reaction_open(m, ex, cur["unit"], r_act, "before", notes):
                env.out += notes
                cur["skipped"] = True
                env.out.append(m.unit_tag(cur["unit"]) + f"- "
                               f"{_name(m, cur['unit'])}'s `{cur['name']}` "
                               f"is no longer available; it doesn't run.")
                continue
            r = await _half(env, seq, cur["n"], "before", True)
            if r == "suspended":
                return "window"
            m = env.m
            ex = _ex(m, seq)
            cur = next(p for p in ex["picks"] if p["n"] == pick["n"])
            if r == "ok":
                _check_cancel(m, ex, cur)
            else:
                cur["failed"] = True
        ex = _ex(env.m, seq)
        ex["phase"] = "body"
    if ex["phase"] == "body":
        r = await _body_phase(env, seq)
        if r == "suspended":
            return "window"
        ex = _ex(env.m, seq)
        ex["phase"] = "after_open"
    if ex["phase"] == "after_open":
        ex = _ex(env.m, seq)
        if ex.get("refunded") or ex.get("gone"):
            ex["units"], ex["unit_order"] = {}, []
            ex["phase"] = "after"
        elif _open(env, seq, "after"):
            await env.flush()
            return "window"
        ex = _ex(env.m, seq)
        ex["phase"] = "after"
    if ex["phase"] == "after":
        if waiting(ex):
            return "window"
        ex["phase"] = "after_run"
    if ex["phase"] == "after_run":
        r = await _after_run(env, seq)
        if r == "suspended":
            return "window"
        ex = _ex(env.m, seq)
        ex["phase"] = "done"
    ex = _ex(env.m, seq)
    env.finished[seq] = ex.get("outcome", "cancelled")
    await env.flush()
    return "done"


async def _body_phase(env: _Env, seq: int) -> str:
    """The action itself after its before halves: cancelled, or re-read
    and its cost and body run. "ok" or "suspended"."""
    from action import (run_action, ActionEngineFault, _rollback_match,
                        REACT_ORIGIN)
    m = env.m
    ex = _ex(m, seq)
    act = _incoming_action(m, ex)
    if act is None or ex["eff_actor"] not in m.entities:
        ex["outcome"] = ex["ended"] = "cancelled"
        env.out.append(f"⚠️ {_name(m, ex['actor'])}'s `{ex['name']}` doesn't "
                       f"resolve: the action or its unit is gone.")
        return "ok"
    origin = (int(ex.get("depth", 0)), True)
    if ex.get("cancelled"):
        return await _cancelled(env, seq, act, origin)
    if not ex.get("reread"):
        _reread(m, ex, act, ex.get("mount") or None, env.out)
        ex["reread"] = True
    await env.flush()
    affected = list(ex.get("affected") or [])
    copies = ex.get("incoming") or {}
    key = affected[0] if len(affected) == 1 else ""
    preset = _plain(copies.get(key, copies.get("", {})))
    extra_ctx = dict(ex.get("mount") or {})
    extra_ctx.update({"affected": affected,
                      "incoming": IncomingProxy(m, seq, key, True)})
    bindings = _bindings(m, seq, True, False)
    bout: Dict[str, Any] = {}
    fout: Dict[str, Any] = {}
    target = target_from_json(ex["target_type"], jcopy(ex.get("target")))
    args = dict(ex.get("args") or {})
    answers = list(ex.get("answers") or [])

    async def run():
        otoken = REACT_ORIGIN.set(origin)
        try:
            with env.runtime():
                return await run_action(
                    act, actor_id=ex["eff_actor"], target=target, args=args,
                    match=env.m, mgr=env.mgr, ctx=env.ctx, answers=answers,
                    extra_ctx=extra_ctx, steps=("cost", "body"),
                    preset_locals=preset, skip_checks=True,
                    extra_bindings=bindings, body_out=bout, fail_out=fout)
        except ActionEngineFault as ef:
            return False, ("", ef.message)
        finally:
            REACT_ORIGIN.reset(otoken)
    st, res = await _run_step(env, seq, "body", run)
    if st == "suspended":
        return "suspended"
    ok, why = res
    m = env.m
    ex = _ex(m, seq)
    if not ok:
        reason, message = why if isinstance(why, tuple) else ("", why)
        tag = f"[{reason}] " if reason else ""
        if fout.get("refund") and seq in env.start:
            # fail(refund): the whole exchange goes back to before its
            # reactions ran; nothing is recorded as used.
            start, mark = env.start[seq]
            keep_ex = jcopy(ex)
            _rollback_match(m, env.mgr, start)
            m.history.rewind(mark)
            m = env.m
            for i, x in enumerate(m.reaction_stack):
                if x.get("seq") == seq:
                    m.reaction_stack[i] = keep_ex
            ex = keep_ex
            for p in ex.get("picks") or []:
                p["refunded"] = True
            ex["refunded"] = True
            env.out.append(f"❌ action `{act.name}`: {tag}{message} "
                           f"(refunded: the reactions are undone too)")
        else:
            note = (" (the reactions stay: the exchange paused for another "
                    "window since they ran)" if fout.get("refund") else "")
            env.out.append(f"❌ action `{act.name}`: {tag}{message}{note}")
        ex["outcome"], ex["ended"] = "cancelled", "failed"
        return "ok"
    ex["outcome"] = ex["ended"] = "resolved"
    body_vals, _dropped = _storable_locals(bout)
    for k in list(ex.get("incoming") or {}):
        c = ex["incoming"][k]
        for name, v in body_vals.items():
            if name not in CONTROL_KEYS:
                c[name] = jcopy(v)
    env.out.append(f"`{ex['actor']}` used action `{act.name}`.")
    return "ok"


async def _cancelled(env: _Env, seq: int, act: Any,
                     origin: Tuple[int, bool]) -> str:
    from action import run_action, ActionEngineFault, REACT_ORIGIN
    m = env.m
    ex = _ex(m, seq)
    by = ex.get("cancelled_by") or ["", ""]
    if not ex.get("cancel_noted"):
        who = f"{_name(m, by[0])}'s `{by[1]}`" if by[0] else "a reaction"
        env.out.append(f"🚫 {_name(m, ex['actor'])}'s `{ex['name']}` is "
                       f"cancelled by {who}.")
        ex["cancel_noted"] = True
    pay = act.cancelled_action_cost or str(
        m.rules.get("cancelled_action_cost", "refund"))
    if pay == "pay" and act.cost:
        await env.flush()
        target = target_from_json(ex["target_type"], jcopy(ex.get("target")))
        preset = _plain((ex.get("incoming") or {}).get("", {}))

        async def run():
            otoken = REACT_ORIGIN.set(origin)
            try:
                with env.runtime():
                    return await run_action(
                        act, actor_id=ex["eff_actor"], target=target,
                        args=dict(ex.get("args") or {}), match=env.m,
                        mgr=env.mgr, ctx=env.ctx,
                        extra_ctx=ex.get("mount") or None, steps=("cost",),
                        finish=False, skip_checks=True,
                        preset_locals=preset)
            except ActionEngineFault as ef:
                return False, ("", ef.message)
            finally:
                REACT_ORIGIN.reset(otoken)
        st, res = await _run_step(env, seq, "cost", run)
        if st == "suspended":
            return "suspended"
        ok, why = res
        if not ok:
            env.out.append(f"⚠️ `{ex['name']}`'s cost failed: "
                           f"{why[1] if isinstance(why, tuple) else why}")
    m = env.m
    ex = _ex(m, seq)
    ex["outcome"] = ex["ended"] = "cancelled"
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
    return "ok"


async def _after_run(env: _Env, seq: int) -> str:
    """Every after half in one order: the continuations of before picks
    (their after_on permitting) and the after-only picks."""
    m = env.m
    ex = _ex(m, seq)
    runs: List[Dict[str, Any]] = []
    for p in ex.get("picks") or []:
        if any(p.get(k) for k in ("refunded", "skipped", "failed")):
            continue
        if p.get("phase") == "before":
            if not p.get("ran") or p.get("after_done"):
                continue
            act = _find_action(m, p["unit"], p["path"], p.get("via"))
            if act is None or not act.reaction.after:
                continue
            want = act.reaction.after_on
            if want != "any" and want != unit_outcome(ex, p["unit"], m):
                continue
        elif p.get("ran"):
            continue
        runs.append(p)
    for pick in _ordered(m, ex, runs, "after"):
        m = env.m
        ex = _ex(m, seq)
        cur = next((p for p in ex["picks"] if p["n"] == pick["n"]), None)
        if cur is None:
            continue
        if cur.get("phase") == "after":
            r_act = _find_action(m, cur["unit"], cur["path"], cur.get("via"))
            notes: List[str] = []
            if r_act is None or not _reaction_open(
                    m, ex, cur["unit"], r_act, "after", notes):
                env.out += notes
                cur["skipped"] = True
                env.out.append(m.unit_tag(cur["unit"]) + f"- "
                               f"{_name(m, cur['unit'])}'s `{cur['name']}` "
                               f"is no longer available; it doesn't run.")
                continue
        pays = cur.get("phase") == "after"
        r = await _half(env, seq, cur["n"], "after", pays)
        if r == "suspended":
            return "suspended"
        m = env.m
        ex = _ex(m, seq)
        cur = next(p for p in ex["picks"] if p["n"] == pick["n"])
        if cur.get("phase") == "before":
            cur["after_done"] = True
        elif r != "ok":
            cur["failed"] = True
    return "ok"


def _record_child(m: Any, child: Dict[str, Any]) -> None:
    """A react point's window closed: its picks go to the paused step of
    the exchange below, which then runs again."""
    pt = child.get("point") or {}
    parent = _ex(m, pt.get("parent"))
    phase = "after" if child.get("phase") == "after" else "before"
    picks = [p for p in child.get("picks") or [] if p.get("phase") == phase]
    _drop(m, child.get("seq"))
    if parent is None or not isinstance(parent.get("suspended"), dict):
        return
    rec = parent["suspended"].setdefault("records", {}).setdefault(
        str(pt.get("k")), {})
    rec[phase] = [{k: jcopy(v) for k, v in p.items()
                   if k not in ("ran", "refunded", "skipped", "failed",
                                "after_done")} for p in picks]


async def _drive(env: _Env) -> None:
    """Run the stack (and then the queue) until a window is open or nothing
    is left; each window that opens is announced."""
    for _guard in range(10000):
        m = env.m
        if not m.reaction_stack:
            m._reaction_opened = None
            if not m.reaction_queue:
                return
            ex = m.reaction_queue.pop(0)
            m.reaction_stack.append(ex)
            if _open(env, ex["seq"], "before"):
                await env.flush()
                await _announce(env)
                return
            continue
        top = m.reaction_stack[-1]
        if top.get("point"):
            if waiting(top):
                await _announce(env)
                return
            _record_child(m, top)
            continue
        r = await _advance(env, top["seq"])
        await env.flush()
        if r == "window":
            await _announce(env)
            return
        m = env.m
        _drop(m, top["seq"])
        m._reaction_opened = None


async def _announce(env: _Env) -> None:
    m = env.m
    m._reaction_opened = time.time()
    lines = env.window or (window_text(m, m.reaction_exchange)
                           if m.reaction_exchange else [])
    env.window = []
    if lines:
        await env.ctx.send("\n".join(lines))


# ----------------------------------------------------------------------
# Starting an exchange
# ----------------------------------------------------------------------

async def _prepare(ctx: Any, mgr: Any, m: Any, action: Any, eff: str,
                   target: Any, args: Dict[str, Any], answers: List[Any],
                   extra_ctx: Optional[Dict[str, Any]]):
    """The action's checks and `prepare`: (ok, why, locals, target,
    answers used)."""
    from action import run_action, ActionEngineFault
    pout: Dict[str, Any] = {}
    tout: Dict[str, Any] = {}
    try:
        ok, why = await run_action(
            action, actor_id=eff, target=target, args=args, match=m,
            mgr=mgr, ctx=ctx, answers=answers, extra_ctx=extra_ctx,
            steps=("prepare",), finish=False, prepare_out=pout,
            target_out=tout)
    except ActionEngineFault as ef:
        return False, ("", ef.message), {}, target, 0
    return ok, why, pout, tout.get("target", target), \
        int(tout.get("answers_used", 0))


def _new_exchange(m: Any, action: Any, *, actor_id: str, eff: str,
                  target: Any, args: Dict[str, Any], answers: List[Any],
                  extra_ctx: Optional[Dict[str, Any]], via: Optional[str],
                  rider_mode: bool, pout: Dict[str, Any], channel: str,
                  depth: int, notes: List[str]
                  ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The exchange of a prepared action, or (None, refusal)."""
    bad = [k for k in pout if k in CONTROL_KEYS]
    if bad:
        return None, (f"its `prepare` can't use `{bad[0]}` as a local: it "
                      f"is a key of `incoming` the reactions set.")
    base, dropped = _storable_locals(pout)
    if dropped:
        notes.append(f"⚠️ `{action.name}`'s `prepare` holds "
                     + ", ".join(f"`{d}`" for d in dropped)
                     + ", which the reaction window can't keep (not a "
                       "value a var could store): the body won't see "
                     + ("it." if len(dropped) == 1 else "them."))
    try:
        affected = _affects(m, action, eff, target, args)
    except VTTError as err:
        return None, str(err)
    m.reaction_seq += 1
    ex: Dict[str, Any] = {
        "seq": m.reaction_seq, "phase": "before", "depth": depth,
        "actor": actor_id, "eff_actor": eff,
        "holder_unit": actor_id, "path": action.full_path, "via": via,
        "rider_mode": rider_mode, "mount": jcopy(extra_ctx),
        "name": action.name, "tags": list(action.tags),
        "target_type": action.target_type,
        "target": target_to_json(action.target_type, target),
        "args": _own_value(dict(args)), "answers": list(answers),
        "affected": list(affected), "first_affected": list(affected),
        "dropped": [], "incoming": {"": base},
        "picks": [], "pick_seq": 0, "channel": channel,
    }
    for u in affected:
        ex["incoming"][u] = jcopy(base)
    return ex, None


async def begin(ctx: Any, mgr: Any, m: Any, action: Any, *,
                actor_id: str, eff_actor_id: str, target: Any,
                args: Dict[str, Any], answers: List[Any],
                extra_ctx: Optional[Dict[str, Any]],
                via: Optional[str], rider_mode: bool) -> None:
    """A typed action that may open a reaction window (design §3)."""
    with _Runtime(m, ctx, mgr):
        ok, why, pout, target, used = await _prepare(
            ctx, mgr, m, action, eff_actor_id, target, args, answers,
            extra_ctx)
    if not ok:
        reason, message = why
        tag = f"[{reason}] " if reason else ""
        return await ctx.send(f"❌ action `{action.name}`: {tag}{message}")
    m = mgr.matches.get(m.id, m)
    notes: List[str] = []
    ex, refusal = _new_exchange(
        m, action, actor_id=actor_id, eff=eff_actor_id, target=target,
        args=args, answers=answers[used:], extra_ctx=extra_ctx, via=via,
        rider_mode=rider_mode, pout=pout,
        channel=str(getattr(ctx, "channel_key", "")), depth=0, notes=notes)
    if ex is None:
        return await ctx.send(f"❌ action `{action.name}`: {refusal}")
    m.reaction_stack.append(ex)
    env = _Env(ctx, mgr, m)
    env.out += notes
    if _open(env, ex["seq"], "before"):
        await env.flush()
        return await _announce(env)
    await _drive(env)


# ----------------------------------------------------------------------
# Actions a formula starts (#36: react points and the queue)
# ----------------------------------------------------------------------

def _sync(coro) -> Any:
    from action import _sync_dispatch_returning
    return _sync_dispatch_returning(coro)


def _plain_use(m: Any, mgr: Any, ctx: Any, action: Any, eid: str,
               target: Any, args: Dict[str, Any],
               why: str) -> Tuple[bool, Any]:
    """Run the action at once with no window (a formula outside any
    resolving step, or past reaction_depth_limit)."""
    from action import run_action, _BufferCtx
    buf = ctx if hasattr(ctx, "out") else _BufferCtx.for_ctx(ctx)
    top = m._action_depth == 0
    ok, why_f = _sync(run_action(action, actor_id=eid, target=target,
                                 args=args, match=m, mgr=mgr, ctx=buf))
    if top and buf is not ctx and buf.out:
        m.surface_log(list(buf.out))
    if ok and why:
        note = unoffered_note(m, action, eid, target, why)
        if note:
            m.surface_log([note])
    return ok, why_f


def formula_use(m: Any, mgr: Any, ctx: Any, action: Any, eid: str,
                target: Any, args: Dict[str, Any]) -> Tuple[Any, Any]:
    """use_action(): (result, why). The result is True / False as the
    action resolved or failed, or None while it waits on a window."""
    from action import REACT_STEP, REACT_ORIGIN
    depth, reactable = REACT_ORIGIN.get() or (0, True)
    try:
        limit = int(m.rules.get("reaction_depth_limit", 2))
    except (TypeError, ValueError):
        limit = 2
    step = REACT_STEP.get()
    if not has_holders(m):
        return _plain_use(m, mgr, ctx, action, eid, target, args, "")
    if not reactable:
        return _plain_use(m, mgr, ctx, action, eid, target, args,
                          "its reaction isn't reactable")
    if depth > limit:
        return _plain_use(m, mgr, ctx, action, eid, target, args,
                          "reaction_depth_limit")
    if step is None:
        if m._action_depth > 0:
            return _plain_use(m, mgr, ctx, action, eid, target, args,
                              "formula")
        return _queue_use(m, mgr, ctx, action, eid, target, args, depth)
    return _point_use(m, mgr, ctx, action, eid, target, args, depth, step)


def _point_use(m: Any, mgr: Any, ctx: Any, action: Any, eid: str,
               target: Any, args: Dict[str, Any], depth: int,
               step: Dict[str, Any]) -> Tuple[Any, Any]:
    """An automatic react point: the action resolves here, inside the
    step, with the picks its windows got; a window not yet answered stops
    the step (ReactPointNeeded)."""
    from action import _BufferCtx
    k = step["counter"][0]
    step["counter"][0] += 1
    rec = (step.get("records") or {}).get(str(k)) or {}
    buf = ctx if hasattr(ctx, "out") else _BufferCtx.for_ctx(ctx)
    ok, why, pout, target, _used = _sync(_prepare(
        buf, mgr, m, action, eid, target, args, [], None))
    if not ok:
        return False, why
    parent = _ex(m, step["seq"]) or {}
    notes: List[str] = []
    ex, refusal = _new_exchange(
        m, action, actor_id=eid, eff=eid, target=target, args=args,
        answers=[], extra_ctx=None, via=None, rider_mode=False, pout=pout,
        channel=str(getattr(buf, "channel_key", "")), depth=depth,
        notes=notes)
    if ex is None:
        return False, ("", refusal)
    ex["point"] = {"parent": step["seq"], "k": k,
                   "by": step.get("by") or parent.get("name", "")}
    m.reaction_stack.append(ex)
    env = _Env(buf, mgr, m, inline=True, rec=rec)
    env.out += notes
    try:
        _open(env, ex["seq"], "before")
        _sync(_advance(env, ex["seq"]))
    finally:
        _drop(env.m, ex["seq"])
    _sync(env.flush())
    return env.finished.get(ex["seq"]) == "resolved", None


def _queue_use(m: Any, mgr: Any, ctx: Any, action: Any, eid: str,
               target: Any, args: Dict[str, Any],
               depth: int) -> Tuple[Any, Any]:
    """An action a formula outside any action starts (a passive, a
    watcher): its window opens now and the formula carries on (None), or
    it waits its turn in the queue while another window is open."""
    from action import _BufferCtx
    buf = _BufferCtx.for_ctx(ctx)
    ok, why, pout, target, _used = _sync(_prepare(
        buf, mgr, m, action, eid, target, args, [], None))
    if buf.out:
        m.surface_log(list(buf.out))
        buf.out.clear()
    if not ok:
        return False, why
    notes: List[str] = []
    ex, refusal = _new_exchange(
        m, action, actor_id=eid, eff=eid, target=target, args=args,
        answers=[], extra_ctx=None, via=None, rider_mode=False, pout=pout,
        channel=str(getattr(buf, "channel_key", "")), depth=depth,
        notes=notes)
    if ex is None:
        return False, ("", refusal)
    if notes:
        m.surface_log(notes)
    if m.reaction_stack:
        m.reaction_queue.append(ex)
        m.surface_log([f"⏳ {_action_line(m, ex)} waits: another reaction "
                       f"window is open, and its own opens after."])
        return None, None
    m.reaction_stack.append(ex)
    env = _Env(buf, mgr, m)
    opened = _open(env, ex["seq"], "before")
    if opened:
        _sync(env.flush())
        _sync(_announce(env))
    else:
        _sync(_drive(env))
    if buf.out:
        m.surface_log(list(buf.out))
    if ex["seq"] in env.finished:
        return env.finished[ex["seq"]] == "resolved", None
    return None, None


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
    await _drive(_Env(ctx, mgr, m))


async def maybe_close(ctx: Any, mgr: Any, m: Any) -> None:
    m = mgr.matches.get(m.id, m)
    ex = m.reaction_exchange
    if ex is None:
        return
    _refresh_done(m, ex)
    if not waiting(ex):
        await _drive(_Env(ctx, mgr, m))


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
# The note on an unoffered action, reactions_used
# ----------------------------------------------------------------------

def unoffered_note(m: Any, act: Any, actor: str, target: Any,
                   why: str = "formula") -> Optional[str]:
    """A note for an action a formula started that opened no window (`why`:
    "formula", a formula inside an action outside any reaction step;
    "reaction_depth_limit"; or a reaction that isn't reactable) when a unit
    holds a reaction that would answer it (reacts_to, reacts_on;
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
            if why == "reaction_depth_limit":
                return (f"ℹ️ `{act.name}` ran past reaction_depth_limit, so "
                        f"reactions to it weren't offered.")
            if why == "formula":
                return (f"ℹ️ `{act.name}` ran from a formula, so reactions "
                        f"to it weren't offered.")
            return (f"ℹ️ `{act.name}` ran from a reaction that isn't "
                    f"reactable, so reactions to it weren't offered.")
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
    cur_seq = (m.reaction_stack[-1].get("seq") if m.reaction_stack
               else m.reaction_seq)
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
        if since == "action" and r.get("seq") != cur_seq:
            continue
        if tag is not None:
            try:
                if not tag_filter_matches(r.get("tags") or [], tag):
                    continue
            except VTTError as err:
                raise VTTError(f"reactions_used(tag=): {err}")
        n += 1
    return n
