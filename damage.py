"""The damage pipeline (#25) and its healing twin.

The design is docs/damage_reactions_design.md §10. A PACKET is a list of
COMPONENTS, each `{amount, pool, tags}` (the var it lowers / raises, its
types), plus the hit's own tags, the unit dealing it (the attacker or the
healer, may be None) and its source. `run_packet` takes one packet to one
unit:

1. The system's ordered steps run in two phases: every step of the first
   side (attacker / healer), then every step of the second (defender /
   receiver), each phase in its list order (`order`, then name). A step
   runs on the components its tag filter matches (their own tags, the
   packet's tags and `pool:<var>`, a reserved derived tag); with none
   matched it is skipped. Its formula edits them directly
   (`c.amount = ...`, action-mode attribute writes on ComponentProxy) or
   through the `step_` helpers (formula.py), which read STEP_FRAME. A step
   whose formula errors is skipped with a warning and the packet goes on
   (its edits to the components undone; its writes to the match stay, as
   a failed passive's do).
2. Per pool, the components' sum, at least the component floor rule,
   rounded once (the rounding rule): `taken`. The first phase's result is
   `raw`, the same way.
3. Unless it is a forecast: each pool written once through write_var, the
   target's death check deferred until all of them are written; a pool
   the target doesn't hold is dropped; an indestructible unit's hp var is
   not written. Then the damage records.

A forecast runs steps 1-2 and puts the match back as it was, RNG included.
"""

from __future__ import annotations

import contextvars
import math
from typing import Any, Dict, List, Optional, Tuple

from logic import (VTTError, NotFound, jcopy, check_tag, check_tag_filter,
                   tag_filter_matches, VAR_NAME_RULES, _check_var_path_rule,
                   _dict_path_get)


def unit_var(e: Any, path: str) -> Any:
    found, v = _dict_path_get(e.vars, path)
    return v if found else None


# Per kind: the library, the two sides (phase order), the binding names a
# step sees and the result / record names.
KINDS: Dict[str, Dict[str, Any]] = {
    "damage": {
        "lib": "damage_steps",
        "sides": ("attacker", "defender"),
        "units": ("attacker", "defender"),
        "source": "damage_source",
        "tags": "damage_tags",
        "all": "damage_components",
        "pool_rule": "damage_default_pool",
        "filter_rule": "damage_step_default_filter",
        "round_rule": "damage_rounding",
        "floor_rule": "damage_component_floor",
        "raw": "damage_raw",
        "taken": "damage_taken",
        "lost": "damage_lost",
        "rec_target": "damage_taken",
        "rec_dealer": "damage_dealt",
        "true_tag": "true_damage",
        "noun": "damage",
        "verb": "takes",
    },
    "healing": {
        "lib": "healing_steps",
        "sides": ("giver", "receiver"),
        # (`healer` / `receiver` would be bindings, so no unit could take
        # those ids.)
        "units": ("healing_giver", "healing_receiver"),
        "source": "healing_source",
        "tags": "healing_tags",
        "all": "healing_components",
        "pool_rule": "healing_default_pool",
        "filter_rule": "healing_step_default_filter",
        "round_rule": "healing_rounding",
        "floor_rule": "healing_component_floor",
        "raw": "healing_raw",
        "taken": "healing_received",
        "lost": "healing_gained",
        "rec_target": "healing_received",
        "rec_dealer": "healing_given",
        "true_tag": "true_healing",
        "noun": "healing",
        "verb": "receives",
    },
}

# A component's derived pool tag: `pool:<var path>`. No tag anyone writes
# may start with it (user call: a reserved, engine-made tag).
POOL_TAG_PREFIX = "pool:"

# An action's `prepare` running (reactions._prepare): the forecasts it
# makes record here, per target, what they came to ({target: {damage_raw:
# {pool: n}, damage_taken: ...}}, summed over several), plus the RNG
# position where the first one began (`rng`), for the target's `incoming`
# copy and the dice rewind before the body (design §10.1).
PREPARE_FORECAST: "contextvars.ContextVar[Optional[Dict[str, Any]]]" = (
    contextvars.ContextVar("vtt_damage_prepare", default=None))
# The exchange whose body is running (match id, seq): a step's `incoming`
# is the defender's copy of it.
PACKET_EXCHANGE: "contextvars.ContextVar[Optional[Tuple[str, int]]]" = (
    contextvars.ContextVar("vtt_damage_exchange", default=None))

# The step being run: its matched components, the whole packet and the
# kind, for the step_ helpers. None outside a step.
STEP_FRAME: "contextvars.ContextVar[Optional[Dict[str, Any]]]" = (
    contextvars.ContextVar("vtt_damage_step", default=None))


# ----------------------------------------------------------------------
# Steps
# ----------------------------------------------------------------------

def kind_info(kind: str) -> Dict[str, Any]:
    if kind not in KINDS:
        raise VTTError(f"unknown pipeline `{kind}` (damage or healing).")
    return KINDS[kind]


def check_component_tag(tag: Any) -> str:
    t = check_tag(tag)
    if t.lower().startswith(POOL_TAG_PREFIX):
        raise VTTError(f"`{t}` can't be a tag: `{POOL_TAG_PREFIX}` tags are "
                       f"made by the engine from a component's pool.")
    return t


def check_tags(raw: Any, what: str = "tags") -> List[str]:
    """A tag list from a list or comma / space text."""
    if raw is None:
        return []
    if isinstance(raw, str):
        words = [w for w in raw.replace(",", " ").split() if w]
    elif isinstance(raw, (list, tuple)):
        words = list(raw)
    else:
        raise VTTError(f"{what}: a list of tags or `a,b` text, got "
                       f"{type(raw).__name__}.")
    out: List[str] = []
    for w in words:
        t = check_component_tag(w)
        if t not in out:
            out.append(t)
    return out


def normalize_step(kind: str, d: Dict[str, Any]) -> Dict[str, Any]:
    """A stored step in its checked shape; VTTError when it can't be one."""
    info = kind_info(kind)
    if not isinstance(d, dict):
        raise VTTError("a step is a dict.")
    side = str(d.get("side") or info["sides"][1]).lower()
    if side not in info["sides"]:
        raise VTTError(f"a {info['noun']} step's side is "
                       f"{' or '.join(info['sides'])}, not `{side}`.")
    formula = d.get("formula")
    if not isinstance(formula, str) or not formula.strip():
        raise VTTError("a step needs a formula.")
    flt = d.get("filter")
    if flt in ("", None):
        flt = None
    else:
        flt = check_tag_filter(flt)
    try:
        order = float(d.get("order", 0))
    except (TypeError, ValueError):
        raise VTTError("a step's order is a number.")
    if not math.isfinite(order):
        raise VTTError("a step's order is a finite number.")
    out: Dict[str, Any] = {"side": side, "formula": formula,
                           "order": int(order) if order == int(order)
                           else order}
    if flt is not None:
        out["filter"] = flt
    desc = d.get("description")
    if isinstance(desc, str) and desc.strip():
        out["description"] = desc.strip()
    return out


def ordered_steps(steps: Dict[str, Dict[str, Any]],
                  kind: str) -> List[Tuple[str, Dict[str, Any]]]:
    """The steps in run order: the first side's, then the second's, each
    by `order`, then name."""
    sides = kind_info(kind)["sides"]
    return sorted(steps.items(),
                  key=lambda kv: (sides.index(kv[1].get("side", sides[1]))
                                  if kv[1].get("side") in sides else 1,
                                  float(kv[1].get("order", 0)), kv[0]))


def place_order(steps: Dict[str, Dict[str, Any]], kind: str, side: str,
                before: Optional[str] = None,
                after: Optional[str] = None,
                skip: Optional[str] = None) -> float:
    """An `order` putting a step of `side` before / after another step of
    the same side, else at the end of its side."""
    same = [(n, s) for n, s in ordered_steps(steps, kind)
            if s.get("side") == side and n != skip]
    orders = [float(s.get("order", 0)) for _n, s in same]
    names = [n for n, _s in same]
    ref = before or after
    if ref is not None:
        if ref not in steps:
            raise NotFound(f"no {kind} step `{ref}`.")
        if ref == skip:
            raise VTTError(f"a step can't go before or after itself "
                           f"(`{ref}`).")
        if steps[ref].get("side") != side:
            raise VTTError(f"`{ref}` is a {steps[ref].get('side')} step: a "
                           f"step goes among the steps of its own side.")
        i = names.index(ref)
        if before is not None:
            lo = orders[i - 1] if i > 0 else orders[i] - 20
            hi = orders[i]
        else:
            lo = orders[i]
            hi = orders[i + 1] if i + 1 < len(orders) else orders[i] + 20
        mid = (lo + hi) / 2
        return int(mid) if mid == int(mid) else mid
    return (int(max(orders)) // 10 + 1) * 10 if orders else 10


def renumber(steps: Dict[str, Dict[str, Any]], kind: str) -> None:
    """Orders 10, 20, ... per side, in run order (after a move, so the
    numbers stay short)."""
    count: Dict[str, int] = {}
    for n, s in ordered_steps(steps, kind):
        side = s.get("side")
        count[side] = count.get(side, 0) + 1
        s["order"] = count[side] * 10


# ----------------------------------------------------------------------
# Components
# ----------------------------------------------------------------------

def resolve_pool(m: Any, pool: Any, kind: str) -> str:
    """A component's pool as a var path; `@<rule>` (or no pool: the default
    pool rule) reads the var a var-name rule names."""
    if pool is None or pool == "":
        pool = str(m.rules.get(kind_info(kind)["pool_rule"], "@hp"))
    if not isinstance(pool, str):
        raise VTTError(f"a pool is a var name, got {type(pool).__name__}.")
    pool = pool.strip()
    if pool.startswith("@"):
        rule = pool[1:] + "_var"
        if rule not in VAR_NAME_RULES:
            raise VTTError(f"`{pool}` names no var-name rule.")
        return str(m.rules.get(rule, pool[1:]))
    return _check_var_path_rule("pool", pool)


def _number(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise VTTError(f"{what} is a number, got {v!r}.")
    if not math.isfinite(v):
        raise VTTError(f"{what} is a finite number, got {v!r}.")
    return v


def build_components(m: Any, kind: str, damage: Any, pool: Any = None,
                     tags: Any = None) -> List[Dict[str, Any]]:
    """The packet's components from a number or a list of component
    dicts; `pool` / `tags` fill in a component without its own."""
    base_tags = check_tags(tags)
    if isinstance(damage, (int, float)) and not isinstance(damage, bool):
        items: List[Any] = [{"amount": damage}]
    elif isinstance(damage, (list, tuple)):
        items = list(damage)
        if not items:
            raise VTTError(f"a {kind} packet needs at least one component.")
    elif isinstance(damage, dict):
        items = [damage]
    else:
        raise VTTError(f"the {kind} is a number or a list of components "
                       f"({{amount, pool, tags}}), got {damage!r}.")
    out: List[Dict[str, Any]] = []
    for it in items:
        if isinstance(it, (int, float)) and not isinstance(it, bool):
            it = {"amount": it}
        if not isinstance(it, dict):
            raise VTTError(f"a component is a dict {{amount, pool, tags}} "
                           f"or a number, got {it!r}.")
        extra = [k for k in it if k not in ("amount", "pool", "tags")]
        if extra:
            raise VTTError(f"a component has amount, pool and tags, not "
                           f"`{extra[0]}`.")
        amount = _number(it.get("amount"), "a component's amount")
        own = check_tags(it.get("tags")) if "tags" in it else []
        out.append({"amount": amount,
                    "pool": resolve_pool(m, it.get("pool", pool), kind),
                    "tags": own + [t for t in base_tags if t not in own]})
    return out


def filter_tags(comp: Dict[str, Any], packet_tags: List[str]) -> List[str]:
    out = list(comp["tags"])
    out += [t for t in packet_tags if t not in out]
    out.append(POOL_TAG_PREFIX + comp["pool"])
    return out


def component_text(c: Dict[str, Any], default_pool: Optional[str] = None
                   ) -> str:
    """`12 slash`, `5 fire stamina`: the amount, its tags, its pool when
    it isn't the default one."""
    a = c["amount"]
    amt = (str(int(a)) if isinstance(a, (int, float)) and a == int(a)
           else f"{a:.2f}".rstrip("0").rstrip("."))
    bits = [amt] + list(c.get("tags") or [])
    if c.get("pool") and c["pool"] != default_pool:
        bits.append(f"→{c['pool']}")
    return " ".join(bits)


class ComponentProxy:
    """A component a step's formula reads and edits: `c.amount`, `c.pool`,
    `c.tags` (a copy; assign a new list to change it). Read-only for
    `damage_components` / `healing_components`."""

    __slots__ = ("_c", "_m", "_kind", "_ro")

    def __init__(self, comp: Dict[str, Any], m: Any, kind: str,
                 read_only: bool = False) -> None:
        object.__setattr__(self, "_c", comp)
        object.__setattr__(self, "_m", m)
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_ro", read_only)

    def __getattr__(self, name: str) -> Any:
        c = object.__getattribute__(self, "_c")
        if name == "tags":
            return list(c["tags"])
        if name in ("amount", "pool"):
            return c[name]
        raise AttributeError(f"a component has amount, pool and tags, not "
                             f"`{name}`")

    def __setattr__(self, name: str, value: Any) -> None:
        if object.__getattribute__(self, "_ro"):
            raise AttributeError("these components are read-only: a step "
                                 "edits `components`, the ones it matched")
        c = object.__getattribute__(self, "_c")
        if name == "amount":
            c["amount"] = _number(value, "a component's amount")
        elif name == "pool":
            c["pool"] = resolve_pool(object.__getattribute__(self, "_m"),
                                     value,
                                     object.__getattribute__(self, "_kind"))
        elif name == "tags":
            c["tags"] = check_tags(value)
        else:
            raise AttributeError(f"a component has amount, pool and tags, "
                                 f"not `{name}`")

    def __repr__(self) -> str:
        return component_text(object.__getattribute__(self, "_c"))


# ----------------------------------------------------------------------
# Running a packet
# ----------------------------------------------------------------------

def _rounded(total: float, mode: str) -> int:
    if mode == "ceil":
        return int(math.ceil(total))
    if mode == "round":
        return int(math.floor(total + 0.5))
    return int(math.floor(total))


def pool_totals(m: Any, kind: str,
                comps: List[Dict[str, Any]]) -> Dict[str, int]:
    """Per pool: the components' sum, at least the floor rule, rounded."""
    info = kind_info(kind)
    floor_raw = str(m.rules.get(info["floor_rule"], "0")).strip().lower()
    try:
        floor = None if floor_raw == "none" else float(floor_raw)
    except ValueError:
        floor = 0.0
    mode = str(m.rules.get(info["round_rule"], "floor"))
    sums: Dict[str, float] = {}
    for c in comps:
        sums[c["pool"]] = sums.get(c["pool"], 0) + c["amount"]
    out: Dict[str, int] = {}
    for p, v in sums.items():
        if not math.isfinite(v):
            raise VTTError(f"the {info['noun']} to pool `{p}` adds up past "
                           f"the largest number ({v}).")
        if floor is not None and v < floor:
            v = floor
        out[p] = _rounded(v, mode)
    return out


def _step_error(ex: BaseException) -> str:
    msg = str(ex)
    return msg if msg else type(ex).__name__


def _run_step(m: Any, kind: str, name: str, step: Dict[str, Any],
              comps: List[Dict[str, Any]], packet: Dict[str, Any],
              trace: Optional[List[Dict[str, Any]]],
              notes: List[str]) -> List[Dict[str, Any]]:
    """One step on `comps` (returns the components after it)."""
    from formula import FormulaEngine, EvalCtx, FormulaError
    info = kind_info(kind)
    flt = step.get("filter")
    if flt is None:
        flt = str(m.rules.get(info["filter_rule"], "") or "")
    side = step.get("side")
    self_id = packet["dealer"] if side == info["sides"][0] else \
        packet["target"]
    try:
        matched = [c for c in comps
                   if not flt or tag_filter_matches(
                       filter_tags(c, packet["tags"]), flt)]
    except VTTError as ex:
        notes.append(f"⚠️ {info['noun']} step `{name}`: its filter "
                     f"doesn't read ({ex}); skipped.")
        if trace is not None:
            trace.append({"step": name, "side": side, "filter": flt,
                          "status": "error", "error": str(ex)})
        return comps
    before = jcopy(comps)
    if not matched:
        if trace is not None:
            trace.append({"step": name, "side": side, "filter": flt,
                          "status": "skipped"})
        return comps
    if side == info["sides"][0] and packet["dealer"] is None:
        # A first-side step reads its unit; a packet nobody dealt (a lava
        # tile) has none, so those steps don't apply.
        if trace is not None:
            trace.append({"step": name, "side": side, "filter": flt,
                          "status": "skipped", "why": "no "
                          + info["sides"][0]})
        return comps
    saved = jcopy(comps)
    frame = {"kind": kind, "matched": matched, "comps": comps, "m": m,
             "self": self_id, "packet_tags": list(packet["tags"])}
    first, second = info["units"]
    extras = {
        first: packet["dealer"], second: packet["target"],
        info["source"]: packet["source"],
        info["tags"]: list(packet["tags"]),
        "components": [ComponentProxy(c, m, kind) for c in matched],
        info["all"]: [ComponentProxy(c, m, kind, read_only=True)
                      for c in comps],
        "incoming": _step_incoming(m, packet["target"]),
    }
    token = STEP_FRAME.set(frame)
    try:
        FormulaEngine(m).eval_program(
            step["formula"],
            EvalCtx(this=m.current_entity_id(), target=self_id,
                    extras=extras),
            action_mode=True, action_bindings={})
    except (FormulaError, VTTError, AttributeError, TypeError,
            ValueError, ZeroDivisionError) as ex:
        comps[:] = saved
        notes.append(f"⚠️ {info['noun']} step `{name}` failed and was "
                     f"skipped: {_step_error(ex)}")
        if trace is not None:
            trace.append({"step": name, "side": side, "filter": flt,
                          "status": "error", "error": _step_error(ex),
                          "before": before})
        return comps
    finally:
        STEP_FRAME.reset(token)
    if trace is not None:
        trace.append({"step": name, "side": side, "filter": flt,
                      "status": "ran", "before": before,
                      "after": jcopy(comps)})
    return comps


def _step_incoming(m: Any, target: str) -> Any:
    """A step's `incoming`: the defender's copy of the exchange whose body
    is running (read-only), else None."""
    cur = PACKET_EXCHANGE.get()
    if cur is None or cur[0] != m.id:
        return None
    from reactions import IncomingProxy, _ex
    if _ex(m, cur[1]) is None:
        return None
    return IncomingProxy(m, cur[1], "", False, uid=target)


def _record_forecast(m: Any, kind: str, target: str,
                     result: Dict[str, Any], rng_at: int) -> None:
    """A forecast made in a `prepare` (PREPARE_FORECAST): what it came to,
    added to the target's record, and where the first one's dice began."""
    frame = PREPARE_FORECAST.get()
    if frame is None:
        return
    info = kind_info(kind)
    frame.setdefault("rng", rng_at)
    rec = frame.setdefault("targets", {}).setdefault(target, {})
    for key, part in ((info["raw"], "raw"), (info["taken"], "taken")):
        d = rec.setdefault(key, {})
        for p, v in result[part].items():
            d[p] = d.get(p, 0) + v


def run_packet(m: Any, kind: str, target: str, damage: Any, *,
               pool: Any = None, tags: Any = None, packet_tags: Any = None,
               dealer: Optional[str] = None, source: Optional[str] = None,
               forecast: bool = False, quiet: bool = False,
               trace: Optional[List[Dict[str, Any]]] = None
               ) -> Dict[str, Any]:
    """Take one packet to `target` (module docstring). Returns {raw,
    taken, lost (per pool), components, dropped, notes, log, skipped}."""
    info = kind_info(kind)
    if not isinstance(target, str) or target not in m.entities:
        raise NotFound(f"no unit `{target}` to take {info['noun']}.")
    if dealer is not None and dealer not in m.entities:
        dealer = None
    e = m.entities[target]
    result: Dict[str, Any] = {"kind": kind, "target": target,
                              "dealer": dealer, "source": source,
                              "raw": {}, "taken": {}, "lost": {},
                              "components": [], "dropped": [], "notes": [],
                              "log": [], "skipped": None}
    # A dead unit takes nothing; a destroyed body part still passes hits on
    # to a living body (a broken limb).
    if not (e.is_alive or (e.is_part and m.body_root(e).is_alive)):
        result["skipped"] = "dead"
        result["notes"].append(f"ℹ️ `{target}` is dead: the {info['noun']} "
                               f"isn't dealt.")
        return result
    comps = build_components(m, kind, damage, pool, tags)
    packet = {"target": target, "dealer": dealer, "source": source,
              "tags": check_tags(packet_tags, "packet tags")}
    result["packet_tags"] = list(packet["tags"])
    notes: List[str] = result["notes"]
    pre = mark = rng = None
    if forecast:
        pre = m.to_dict(include_history=False)
        mark = m.history.mark()
        rng = m.rng_position()
    try:
        steps = ordered_steps(getattr(m, info["lib"]), kind)
        first = info["sides"][0]
        raw_done = False
        for name, step in steps:
            if not raw_done and step.get("side") != first:
                result["raw"] = pool_totals(m, kind, comps)
                raw_done = True
            if target not in m.entities:
                break
            comps = _run_step(m, kind, name, step, comps, packet, trace,
                              notes)
        if not raw_done:
            result["raw"] = pool_totals(m, kind, comps)
        result["components"] = jcopy(comps)
        totals = pool_totals(m, kind, comps)
        e = m.entities.get(target)
        # A pool the target doesn't hold (stamina damage on a machine
        # with none): dropped.
        for p in list(totals):
            v = unit_var(e, p) if e is not None else None
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                result["dropped"].append(p)
                notes.append(f"ℹ️ `{target}` has no number at `{p}`: its "
                             f"{info['noun']} there is dropped.")
                del totals[p]
        result["taken"] = totals
    finally:
        if forecast:
            from action import _rollback_match
            _rollback_match(m, None, pre)
            m.history.rewind(mark)
            m.set_rng(pos=rng)
    if forecast:
        _record_forecast(m, kind, target, result, rng)
    if forecast or e is None or target not in m.entities:
        return result
    # A body part's share is worked out before anything is written, so a
    # bad knob (to_main_cap, a percent) refuses the whole hit.
    share = None
    if e.is_part:
        hp_var, mhp_var, _ = e._vital_var_names()
        try:
            share = _plan_share(m, kind, result, unit_var(e, hp_var),
                                unit_var(e, mhp_var))
        except VTTError as ex:
            raise VTTError(f"damage_part `{target}`: {ex}"
                           if kind == "damage" else f"`{target}`: {ex}")
    # The target's death check is held through the writes, the share and
    # the hooks, then runs once (design §10.9): on_damage_taken fires on a
    # killing blow too, with the unit still in the match at 0 hp, and a
    # hook that heals it above 0 saves it. A hook's own writes to the
    # target wait for the same check.
    from logic import PACKET_KILLER
    held = target not in m._death_check_deferred_ids
    if held:
        m._death_check_deferred_ids.add(target)
    try:
        _write_pools(m, kind, e, result, quiet)
        _record(m, kind, result)
        if e.is_part and share:
            _send_share(m, kind, result, share, quiet)
        _fire_hooks(m, kind, result)
    finally:
        if held:
            m._death_check_deferred_ids.discard(target)
    if held and target in m.entities:
        token = PACKET_KILLER.set((m.id, target, result["dealer"]))
        try:
            result["log"].extend(m.check_death(target))
        finally:
            PACKET_KILLER.reset(token)
    if target in m.entities and m.entities[target].is_part:
        _part_destroyed(m, kind, result)
    return result


def _num(v: Any, default: float = 0) -> float:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(v) else default


def _share_percent(m: Any, kind: str, part: Any, comp: Dict[str, Any],
                   packet_tags: List[str]) -> float:
    """The percent of component `comp` a body part sends to its parent.
    Damage: the part's `damage_to_main_percent` var, else the
    part_damage_to_main_percent_default rule. Healing: the part's
    `heal_to_main_percent` var, else the part_heal_to_main_percent_default
    rule when it is a number, else what damage would use (the part's
    damage var, else the damage rule): an explicit heal percent wins over
    following the damage one (user call). A var may be a number or a dict
    of tag filter -> percent with a `default`."""
    def part_var(name: str) -> Any:
        found, v = _dict_path_get(part.vars, name)
        return v if found and v is not None else None

    damage_rule = m.rules.get("part_damage_to_main_percent_default", 0)
    raw, var, rule = None, None, damage_rule
    if kind == "healing":
        raw = part_var("heal_to_main_percent")
        var = "heal_to_main_percent" if raw is not None else None
        hr = str(m.rules.get("part_heal_to_main_percent_default", "same"))
        if hr.strip().lower() != "same":
            rule = hr
            if raw is None:
                raw = hr
    if raw is None:
        raw = part_var("damage_to_main_percent")
        var = "damage_to_main_percent" if raw is not None else None
    if raw is None:
        raw = rule
    if isinstance(raw, dict):
        pick = None
        tags = filter_tags(comp, packet_tags)
        for k, pct in raw.items():
            if k == "default":
                continue
            try:
                if tag_filter_matches(tags, k):
                    pick = pct
                    break
            except VTTError as ex:
                raise VTTError(f"{var}: `{k}` isn't a tag filter ({ex}).")
        if pick is None:
            pick = raw.get("default", rule)
        raw = pick
    try:
        pct = float(raw)
    except (TypeError, ValueError):
        pct = float("nan")
    if not math.isfinite(pct):
        where = var if var else "the to-main percent rule"
        raise VTTError(f"{where}: a to-main percent is a finite number, got "
                       f"{raw!r}.")
    return pct


def _plan_share(m: Any, kind: str, result: Dict[str, Any], pre_hp: Any,
                pre_max: Any) -> List[Dict[str, Any]]:
    """A body part's share to its parent (design §10.5): each component it
    takes above 0, times its percent (the damage share first capped as a
    whole by to_main_cap from the part's hp before the hit), tagged
    true_damage / true_healing under the share-mode rule `true`. [] when
    there is nothing to send."""
    info = kind_info(kind)
    part = m.entities.get(result["target"])
    if part is None or not part.part_of or part.part_of not in m.entities:
        return []
    comps = [c for c in result["components"] if c["amount"] > 0]
    if not comps:
        return []
    tags = list(result.get("packet_tags") or [])
    factor = 1.0
    if kind == "damage":
        cap = str(part.vars.get("to_main_cap",
                                m.rules.get("part_to_main_cap_default",
                                            "max_hp")))
        whole = sum(c["amount"] for c in comps)
        base = m._part_transfer_base(whole, int(_num(pre_hp)),
                                     int(_num(pre_max)), cap)
        factor = base / whole
    mode = str(m.rules.get(
        "part_damage_share_mode" if kind == "damage"
        else "part_heal_share_mode", "true"))
    share: List[Dict[str, Any]] = []
    for c in comps:
        pct = _share_percent(m, kind, part, c, tags)
        amt = c["amount"] * factor * pct / 100
        if amt == 0:
            continue
        ctags = list(c["tags"])
        if mode == "true" and info["true_tag"] not in ctags:
            ctags.append(info["true_tag"])
        share.append({"amount": amt, "pool": c["pool"], "tags": ctags})
    return share


def _send_share(m: Any, kind: str, result: Dict[str, Any],
                share: List[Dict[str, Any]], quiet: bool) -> None:
    """The share goes to the parent as a packet of its own: the hit's
    dealer and source, its packet tags plus `part_share`."""
    info = kind_info(kind)
    part = m.entities.get(result["target"])
    if part is None or not part.part_of or part.part_of not in m.entities:
        return
    tags = list(result.get("packet_tags") or [])
    res = run_packet(m, kind, part.part_of, share,
                     packet_tags=tags + (["part_share"]
                                         if "part_share" not in tags else []),
                     dealer=result["dealer"], source=result["source"],
                     quiet=quiet)
    result["share"] = {"to": part.part_of, info["taken"]: dict(res["taken"])}
    result["log"].extend(res["notes"] + res["log"])


def _part_destroyed(m: Any, kind: str, result: Dict[str, Any]) -> None:
    """A body part a damage packet brought to 0 hp is destroyed (its
    on_death once, a vital part kills its body), unless indestructible:
    after its share reached the body, as damage_part did."""
    from logic import PACKET_KILLER
    if kind != "damage":
        return
    p = m.entities.get(result["target"])
    if p is None or not p.is_part or m.is_indestructible(p):
        return
    hp = unit_var(p, p._vital_var_names()[0])
    if not isinstance(hp, (int, float)) or isinstance(hp, bool) or hp > 0:
        return
    token = PACKET_KILLER.set((m.id, p.id, result["dealer"]))
    try:
        result["log"].extend(m._process_part_death(p))
    finally:
        PACKET_KILLER.reset(token)


def _write_pools(m: Any, kind: str, e: Any, result: Dict[str, Any],
                 quiet: bool = False) -> None:
    """Each pool once through write_var (a write a packet makes logs no
    hp_change entry); then the statuses that break, the packet's line and
    its event-log entry. The target's death check stays held (run_packet
    runs it once, after the hooks)."""
    from logic import PACKET_WRITE
    log: List[str] = result["log"]
    target = e.id
    hp_var = str(m.rules.get("hp_var", "hp"))
    skip_hp = m.is_indestructible(e)
    token = PACKET_WRITE.set((m.id, target))
    try:
        for p, amount in result["taken"].items():
            if target not in m.entities:
                break
            e = m.entities[target]
            if p == hp_var and skip_hp:
                result["lost"][p] = 0
                continue
            old = unit_var(e, p)
            if isinstance(old, bool) or not isinstance(old, (int, float)):
                continue
            new = old - amount if kind == "damage" else old + amount
            if kind == "damage" and p == hp_var and e.is_part and new < 0 \
                    and old >= 0:
                # A body part's hp stops at 0 (a destroyed limb lingers).
                new = 0
            log.extend(e.write_var(p, new) or [])
            now = unit_var(m.entities[target], p) \
                if target in m.entities else new
            if isinstance(now, (int, float)) and not isinstance(now, bool):
                result["lost"][p] = (old - now) if kind == "damage" \
                    else (now - old)
            else:
                result["lost"][p] = amount
    finally:
        PACKET_WRITE.reset(token)
    if kind == "damage" and target in m.entities:
        log.extend(_break_statuses(m, result))
    if target in m.entities:
        _announce_packet(m, kind, result, quiet)
    _log_packet(m, kind, result)


def counted_damage(result: Dict[str, Any], flt: Any) -> float:
    """The damage a packet dealt that a status's damage_filter counts: per
    pool, the pool's damage taken times the share of that pool's positive
    component amounts the filter matches (no filter: everything taken,
    pools above 0 only)."""
    total = 0.0
    tags = list(result.get("packet_tags") or [])
    for p, taken in result["taken"].items():
        if not isinstance(taken, (int, float)) or taken <= 0:
            continue
        if not flt:
            total += taken
            continue
        comps = [c for c in result["components"] if c["pool"] == p
                 and c["amount"] > 0]
        whole = sum(c["amount"] for c in comps)
        if whole <= 0:
            continue
        hit = sum(c["amount"] for c in comps
                  if tag_filter_matches(filter_tags(c, tags), flt))
        total += taken * hit / whole
    return total


def _break_statuses(m: Any, result: Dict[str, Any]) -> List[str]:
    """#235: the target's statuses with `removed_on_damage` go when the
    packet's counted damage is above 0 (removed_by `damage`); those with
    `break_at_damage: N` add it to `damage_counted` and go at N."""
    from logic import STATUS_DAMAGE_COUNT_KEY
    log: List[str] = []
    target = result["target"]
    e = m.entities.get(target)
    if e is None or not e.status:
        return log
    for name in list(e.status):
        e = m.entities.get(target)
        if e is None or name not in e.status:
            continue
        inst = e.status[name]
        rod = m.status_field(inst, name, "removed_on_damage")
        cap = m.status_field(inst, name, "break_at_damage")
        if rod is not True and not (
                isinstance(cap, (int, float)) and not isinstance(cap, bool)):
            continue
        flt = m.status_field(inst, name, "damage_filter")
        try:
            n = counted_damage(result, flt)
        except VTTError as ex:
            log.append(f"⚠️ status `{name}`: its damage_filter doesn't read "
                       f"({ex}); this hit doesn't count.")
            continue
        if n <= 0:
            continue
        before = jcopy(inst)
        gone = rod is True
        if not gone:
            had = inst.get(STATUS_DAMAGE_COUNT_KEY, 0) \
                if isinstance(inst, dict) else 0
            if isinstance(had, bool) or not isinstance(had, (int, float)):
                had = 0
            now = had + n
            if now == int(now):
                now = int(now)
            if now >= cap:
                gone = True
            else:
                inst[STATUS_DAMAGE_COUNT_KEY] = now
                log.extend(m._emit_status_diff(target, name, before,
                                               jcopy(inst)))
                continue
        del e.status[name]
        log.extend(m._emit_status_diff(target, name, before, None,
                                       removed_by="damage"))
    return log


def amounts_text(m: Any, kind: str, taken: Dict[str, Any]) -> str:
    """`18 damage`, `18 damage and 5 stamina damage` (the default pool
    unnamed, pools with nothing left out unless all are)."""
    info = kind_info(kind)
    try:
        dpool = resolve_pool(m, None, kind)
    except VTTError:
        dpool = None
    shown = {p: v for p, v in taken.items() if v} \
        or dict(list(taken.items())[:1])
    bits = [f"{v}" + ("" if p == dpool else f" {p}") + f" {info['noun']}"
            for p, v in shown.items()]
    return " and ".join(bits) or f"no {info['noun']}"


def components_text(m: Any, kind: str, comps: List[Dict[str, Any]]) -> str:
    """`12 slash, 5 fire`."""
    try:
        dpool = resolve_pool(m, None, kind)
    except VTTError:
        dpool = None
    return ", ".join(component_text(c, dpool) for c in comps)


def _announce_packet(m: Any, kind: str, result: Dict[str, Any],
                     quiet: bool) -> None:
    """The packet's line (damage_message_format / healing_message_format),
    tagged as a line about the target, through surface_log; result['line']
    records whether one was shown."""
    info = kind_info(kind)
    result["line"] = None
    if quiet:
        return
    fmt = str(m.rules.get(f"{kind}_message_format", "") or "")
    if not fmt:
        return
    e = m.entities[result["target"]]
    first = info["units"][0]
    dealer = result["dealer"]
    extra = {
        "amounts": amounts_text(m, kind, result["taken"]),
        info["taken"]: dict(result["taken"]),
        info["lost"]: dict(result["lost"]),
        info["raw"]: dict(result["raw"]),
        "components": components_text(m, kind, result["components"]),
        first: (m.say_name(dealer) if dealer is not None else ""),
        "source": result["source"] or "",
    }
    text = m.render_unit_message(fmt, e.id, e.name, e.x, e.y, e.vars,
                                 part_of=e.part_of or "", extra=extra)
    result["line"] = text
    m.surface_log([m.unit_tag(e.id) + text])


def _log_packet(m: Any, kind: str, result: Dict[str, Any]) -> None:
    """The event-log entry (`damage` / `healing`) of a packet."""
    info = kind_info(kind)
    dealer = result["dealer"]
    m.log_event(kind, entity=result["target"],
                name=m._entity_name(result["target"]),
                **{info["sides"][0]: dealer or "",
                   info["sides"][0] + "_name":
                   (m._entity_name(dealer) if dealer else "")},
                source=result["source"] or "",
                amounts=amounts_text(m, kind, result["taken"]),
                taken=dict(result["taken"]), lost=dict(result["lost"]))


def hook_extras(kind: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """The bindings the packet's hooks see."""
    info = kind_info(kind)
    first, second = info["units"]
    return {
        first: result["dealer"], second: result["target"],
        info["source"]: result["source"],
        info["tags"]: list(result.get("packet_tags") or []),
        info["taken"]: dict(result["taken"]),
        info["lost"]: dict(result["lost"]),
        info["all"]: jcopy(result["components"]),
    }


HOOKS = {"damage": ("on_damage_taken", "on_damage_dealt"),
         "healing": ("on_healing_received", "on_healing_given")}


def _fire_hooks(m: Any, kind: str, result: Dict[str, Any]) -> None:
    """on_damage_taken on the target (while it is in the match), then
    on_damage_dealt on the dealer (healing: on_healing_received /
    on_healing_given), under damage_hook_recursion_limit: a packet dealt
    from these hooks nests one deeper, and past the limit packets fire no
    hooks (one warning per chain)."""
    log: List[str] = result["log"]
    try:
        limit = int(m.rules.get("damage_hook_recursion_limit", 16))
    except (TypeError, ValueError):
        limit = 16
    if m._damage_hook_depth >= limit:
        if not m._damage_hook_warned:
            m._damage_hook_warned = True
            log.append(f"⚠️ damage hook recursion limit ({limit}) reached: "
                       f"the rest of this chain's packets fire no damage or "
                       f"healing hooks. Check for a loop (thorns hitting "
                       f"thorns: tag the returned damage and skip it) "
                       f"(damage_hook_recursion_limit).")
        return
    taken_hook, dealt_hook = HOOKS[kind]
    extras = hook_extras(kind, result)
    m._damage_hook_depth += 1
    try:
        if result["target"] in m.entities:
            log.extend(m.fire_hook(taken_hook, target_ids=[result["target"]],
                                   extras=dict(extras, hook_name=taken_hook)))
        d = result["dealer"]
        if d is not None and d in m.entities:
            log.extend(m.fire_hook(dealt_hook, target_ids=[d],
                                   extras=dict(extras, hook_name=dealt_hook)))
    finally:
        m._damage_hook_depth -= 1
        if m._damage_hook_depth == 0:
            m._damage_hook_warned = False


def packet_record(m: Any, kind: str, result: Dict[str, Any]
                  ) -> Dict[str, Any]:
    info = kind_info(kind)
    first, second = info["units"]
    return {
        "kind": kind, first: result["dealer"], second: result["target"],
        "source": result["source"],
        "tags": list(result.get("packet_tags") or []),
        info["raw"]: dict(result["raw"]),
        info["taken"]: dict(result["taken"]),
        info["lost"]: dict(result["lost"]),
        "components": jcopy(result["components"]),
        "turn": m.turns_elapsed,
    }


def _record(m: Any, kind: str, result: Dict[str, Any]) -> None:
    """The unit's last packet taken (and the dealer's last dealt): saved
    with the match, read by last_damage / `!damage last`."""
    info = kind_info(kind)
    rec = packet_record(m, kind, result)
    if result["target"] in m.entities:
        m.damage_records.setdefault(result["target"], {})[
            info["rec_target"]] = rec
    d = result["dealer"]
    if d is not None and d in m.entities:
        m.damage_records.setdefault(d, {})[info["rec_dealer"]] = jcopy(rec)


# ----------------------------------------------------------------------
# The step_ helpers (bound in formula.py), on the running step's frame
# ----------------------------------------------------------------------

def frame() -> Dict[str, Any]:
    f = STEP_FRAME.get()
    if f is None:
        raise VTTError("only inside a damage or healing step: step_ "
                       "functions act on the components the step matched.")
    return f


def step_scale(f: Any) -> None:
    fr = frame()
    k = _number(f, "step_scale(factor)")
    for c in fr["matched"]:
        c["amount"] = c["amount"] * k


def step_add(n: Any) -> None:
    fr = frame()
    k = _number(n, "step_add(n)")
    for c in fr["matched"]:
        c["amount"] = c["amount"] + k


def step_reduce(n: Any) -> float:
    """Takes n off the matched components as a whole, in order, until n is
    used up (never below 0); returns what it took."""
    fr = frame()
    left = _number(n, "step_reduce(n)")
    if left <= 0:
        return 0
    took = 0
    for c in fr["matched"]:
        if left <= 0:
            break
        cut = min(max(c["amount"], 0), left)
        c["amount"] -= cut
        left -= cut
        took += cut
    return took


def step_total() -> float:
    return sum(c["amount"] for c in frame()["matched"])


def step_move(pool: Any) -> None:
    fr = frame()
    p = resolve_pool(fr["m"], pool, fr["kind"])
    for c in fr["matched"]:
        c["pool"] = p


def step_new(amount: Any, pool: Any = None, tags: Any = None) -> None:
    fr = frame()
    comp = build_components(fr["m"], fr["kind"],
                            [{"amount": _number(amount, "step_new(amount)"),
                              **({"pool": pool} if pool is not None else {}),
                              **({"tags": tags} if tags is not None
                                 else {})}])[0]
    fr["comps"].append(comp)
    fr["matched"].append(comp)


def step_drop() -> None:
    fr = frame()
    gone = {id(c) for c in fr["matched"]}
    fr["comps"][:] = [c for c in fr["comps"] if id(c) not in gone]
    fr["matched"][:] = []


def step_mods(stat: Any) -> None:
    """Each matched component folded through the step's unit's modifiers
    for `stat`, with the component's filter tags (apply_mods)."""
    fr = frame()
    if not isinstance(stat, str) or not stat:
        raise VTTError("step_mods(stat): stat is a name.")
    m, who = fr["m"], fr["self"]
    if who is None or who not in m.entities:
        return
    packet_tags = frame_packet_tags(fr)
    for c in fr["matched"]:
        c["amount"] = m.apply_modifiers(who, stat, c["amount"],
                                        filter_tags(c, packet_tags))


def frame_packet_tags(fr: Dict[str, Any]) -> List[str]:
    return list(fr.get("packet_tags") or [])
