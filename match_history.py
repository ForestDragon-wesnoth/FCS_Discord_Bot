"""Per-match save history: round/turn/command autosaves, manual saves, undo.

This module lives separately from logic.py because it's a self-contained
concern (snapshot lifecycle + retention pruning) and would otherwise
bloat the already-large core file. The only coupling back to logic.py
is the Match type, which we import lazily under TYPE_CHECKING to avoid
a circular import.

Key invariants:

- Snapshots are taken BEFORE a state change happens, not after. A turn
  snapshot is taken right after on_turn_start hooks fire, *before* the
  player does anything that turn — restoring it reverts the turn. A
  command snapshot is taken before the mutating command runs (well,
  conceptually — actually we capture pre-state up front then commit the
  snapshot post-dispatch iff the command actually changed something).

- Sequence numbers are monotonic per match and serialize the four
  snapshot kinds against each other. They never reset on restore;
  pruning by sequence (drop everything with seq > restore_point) gives
  linear-history semantics: undo erases the now-orphaned future.

- Retention is rule-driven. The dials live on Match.rules:
    autosave_round_retention          (default -1 / unlimited)
    autosave_turn_retention_rounds    (default 10; round mode)
    autosave_turn_retention_turns     (default 60; ATB, where rounds freeze)
    autosave_command_retention_turns  (default 10)
    autosave_command_retention_max    (default 500; count cap on commands)
  -1 means unlimited, 0 disables that kind entirely, N>0 caps to last N.

- Storage: a round autosave, a manual save and the first autosave hold a
  full match state; a turn or command autosave holds a diff against the
  autosave before it by sequence (state_diff), and pruning one in the middle
  merges its diff into the next (MatchHistory._relink). States share the
  serialized pieces of units, tiles and zones (Entity.ser_dict), so nothing
  may change a snapshot's state in place.

- Manual saves never auto-prune. They live until explicitly deleted or
  the enclosing match is removed.

- This whole structure travels with the Match in memory but is excluded
  from Match.to_dict() by default to keep !store save sizes reasonable.
  Opt in with to_dict(include_history=True) when you do want the
  autosave history persisted (e.g. for a long-term campaign backup).
"""

from __future__ import annotations
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING
import datetime
import json

# Import VTTError eagerly so HistoryError can subclass it. This works
# because logic.py imports us AFTER its exception classes are defined
# (see the late import comment in logic.py).
from logic import VTTError, formula_label
import logic as _logic


def same_value(a: Any, b: Any) -> bool:
    return _logic.same_value(a, b)


# ---- state diffs -----------------------------------------------------------
# A turn or command autosave is stored as a diff against the autosave before
# it (by sequence); a round autosave, a manual save and the first autosave
# are full states (keyframes). A diff names what changed at the grain of the
# serialization caches: top-level fields whole, units by id, maps by name and
# in each map its fields, tiles by cell and zones by name. Its parts are
# {"s": {key: new value}, "d": [deleted keys], "c": {key: nested diff},
# "o": [every key, in order]} ("o" when the keys of an ordered dict were
# added, removed or moved: units act in creation order). Tiles and zones are
# kept sorted, so they carry no order. The event log (append-only, trimmed
# from the front) is {"n": its length before, "drop": entries trimmed from
# the front, "add": [entries appended]}.
_MAP_SPEC: Dict[str, Tuple[str, Any]] = {"tiles": ("tiles", None),
                                         "zones": ("sorted", None)}
_STATE_SPEC: Dict[str, Tuple[str, Any]] = {"entities": ("ordered", None),
                                           "maps": ("ordered", _MAP_SPEC),
                                           "event_log": ("log", None)}
# A keyframe at least this often, so a history without round autosaves (or
# a long round) never replays a long chain of diffs.
KEYFRAME_EVERY = 100


def _diff_dict(a: Dict[str, Any], b: Dict[str, Any],
               spec: Dict[str, Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    sets: Dict[str, Any] = {}
    kids: Dict[str, Any] = {}
    for k, bv in b.items():
        if k not in a:
            sets[k] = bv
            continue
        av = a[k]
        if av is bv:
            continue
        if k in spec and spec[k][0] == "log":
            sub = _diff_log(av, bv)
            if sub is None:
                if not same_value(av, bv):
                    sets[k] = bv
            elif sub["drop"] or sub["add"]:
                kids[k] = sub
        elif k in spec and type(av) is dict and type(bv) is dict:
            kind, valspec = spec[k]
            sub = _diff_keyed(av, bv, kind, valspec)
            if sub:
                kids[k] = sub
        elif not same_value(av, bv):
            sets[k] = bv
    dels = [k for k in a if k not in b]
    if sets:
        out["s"] = sets
    if dels:
        out["d"] = dels
    if kids:
        out["c"] = kids
    return out


def _diff_keyed(a: Dict[str, Any], b: Dict[str, Any], kind: str,
                valspec: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    sets: Dict[str, Any] = {}
    kids: Dict[str, Any] = {}
    added = False
    for k, bv in b.items():
        av = a.get(k, _MISSING) if k in a else _MISSING
        if av is _MISSING:
            sets[k] = bv
            added = True
            continue
        if av is bv:
            continue
        if valspec is not None and type(av) is dict and type(bv) is dict:
            sub = _diff_dict(av, bv, valspec)
            if sub:
                kids[k] = sub
        elif not same_value(av, bv):
            sets[k] = bv
    dels = [k for k in a if k not in b]
    if sets:
        out["s"] = sets
    if dels:
        out["d"] = dels
    if kids:
        out["c"] = kids
    if kind == "ordered" and (added or dels or list(a) != list(b)):
        out["o"] = list(b)
    return out


_MISSING = object()


def _diff_log(a: Any, b: Any) -> Optional[Dict[str, Any]]:
    """`b` as `a` trimmed from the front and appended to, or None."""
    if type(a) is not list or type(b) is not list:
        return None
    n = len(a)
    first = b[0] if b else None
    for k in range(n + 1):
        keep = n - k
        if keep > len(b):
            continue
        if keep and not same_value(a[k], first):
            continue
        if all(same_value(a[k + i], b[i]) for i in range(keep)):
            return {"n": n, "drop": k, "add": b[keep:]}
    return None


def _apply_log(base: Any, d: Dict[str, Any]) -> List[Any]:
    base = base if type(base) is list else []
    return base[d["drop"]:] + list(d["add"])


def _compose_log(d1: Dict[str, Any], d2: Dict[str, Any]) -> Dict[str, Any]:
    n1, a1 = d1["n"], d1["add"]
    left = n1 - d1["drop"]
    if d2["drop"] <= left:
        return {"n": n1, "drop": d1["drop"] + d2["drop"],
                "add": list(a1) + list(d2["add"])}
    return {"n": n1, "drop": n1,
            "add": list(a1[d2["drop"] - left:]) + list(d2["add"])}


def _tile_key(k: str) -> Tuple[int, int]:
    try:
        x, y = k.split(",")
        return int(x), int(y)
    except (ValueError, AttributeError):
        return (0, 0)


def _apply_dict(base: Dict[str, Any], diff: Dict[str, Any],
                spec: Dict[str, Tuple[str, Any]]) -> Dict[str, Any]:
    new = dict(base)
    for k in diff.get("d", ()):
        new.pop(k, None)
    for k, v in diff.get("s", {}).items():
        new[k] = v
    for k, sub in diff.get("c", {}).items():
        kind, valspec = spec[k]
        if kind == "log":
            new[k] = _apply_log(base.get(k), sub)
        else:
            new[k] = _apply_keyed(base.get(k) or {}, sub, kind, valspec)
    return new


def _apply_keyed(base: Dict[str, Any], diff: Dict[str, Any], kind: str,
                 valspec: Any) -> Dict[str, Any]:
    new = dict(base)
    for k in diff.get("d", ()):
        new.pop(k, None)
    sets = diff.get("s", {})
    for k, v in sets.items():
        new[k] = v
    for k, sub in diff.get("c", {}).items():
        new[k] = _apply_dict(base.get(k) or {}, sub, valspec)
    order = diff.get("o")
    if order is not None:
        new = {k: new[k] for k in order if k in new}
    elif kind != "ordered" and (diff.get("d")
                                or any(k not in base for k in sets)):
        keys = sorted(new, key=_tile_key) if kind == "tiles" else sorted(new)
        new = {k: new[k] for k in keys}
    return new


def _compose_dict(d1: Dict[str, Any], d2: Dict[str, Any],
                  spec: Dict[str, Tuple[str, Any]]) -> Dict[str, Any]:
    """d1 then d2 as one diff."""
    return _compose(d1, d2, None, spec)


def _compose(d1: Dict[str, Any], d2: Dict[str, Any], kind: Optional[str],
             spec: Any) -> Dict[str, Any]:
    # kind None: a dict level (its keys named in `spec`); else a keyed
    # level whose values are dict levels of `spec` (or leaves when None).
    sets = dict(d1.get("s", {}))
    dels = list(d1.get("d", ()))
    kids = dict(d1.get("c", {}))
    for k in d2.get("d", ()):
        sets.pop(k, None)
        kids.pop(k, None)
        if k not in dels:
            dels.append(k)
    for k, v in d2.get("s", {}).items():
        sets[k] = v
        kids.pop(k, None)
        if k in dels:
            dels.remove(k)
    for k, sub in d2.get("c", {}).items():
        if kind is None:
            ckind, cspec = spec[k]
            if ckind == "log":
                if k in sets:
                    sets[k] = _apply_log(sets[k], sub)
                elif k in kids:
                    kids[k] = _compose_log(kids[k], sub)
                else:
                    kids[k] = sub
            elif k in sets:
                sets[k] = _apply_keyed(sets[k], sub, ckind, cspec)
            elif k in kids:
                kids[k] = _compose(kids[k], sub, ckind, cspec)
            else:
                kids[k] = sub
        else:
            if k in sets:
                sets[k] = _apply_dict(sets[k], sub, spec)
            elif k in kids:
                kids[k] = _compose(kids[k], sub, None, spec)
            else:
                kids[k] = sub
    out: Dict[str, Any] = {}
    if sets:
        out["s"] = sets
    if dels:
        out["d"] = dels
    if kids:
        out["c"] = kids
    order = d2.get("o", d1.get("o"))
    if order is not None:
        out["o"] = order
    return out


def state_diff(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """The diff that turns match state `a` into `b` (both Match.to_dict).
    Shares b's values: nothing may change it."""
    return _diff_dict(a, b, _STATE_SPEC)


def apply_state_diff(base: Dict[str, Any], diff: Dict[str, Any]
                     ) -> Dict[str, Any]:
    """`base` with `diff` applied, sharing what didn't change."""
    return _apply_dict(base, diff, _STATE_SPEC)


def compose_state_diffs(diffs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One diff doing what `diffs` do in order."""
    out: Dict[str, Any] = {}
    for d in diffs:
        out = _compose_dict(out, d, _STATE_SPEC)
    return out

if TYPE_CHECKING:
    from logic import Match


class HistoryError(VTTError):
    """Raised by MatchHistory operations on invalid input (e.g. undoing
    further back than the retained history allows). Subclasses VTTError
    so the command dispatcher's standard error path surfaces it with
    the ❌ prefix rather than the 💥 'unexpected error' one."""
    pass


def _now_iso() -> str:
    """UTC timestamp for snapshot metadata. ISO 8601 with seconds
    precision; the trailing Z disambiguates the zone for non-Python
    consumers of exported snapshot files."""
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


@dataclass
class Snapshot:
    """An immutable serialized Match state plus enough metadata to
    locate the snapshot in time and prune it under retention rules.

    Field meanings:
      kind                       "round" | "turn" | "command" | "manual"
      sequence                   monotonic per-match counter at snapshot time
      turn_index_at_snapshot     count of total turns elapsed at snapshot
                                 time (used for "keep last N turns of
                                 command snapshots" pruning); 0 before any
                                 turn has begun
      round_at_snapshot          Match.round_number at snapshot time
      active_index               Match.active_index at snapshot time
      active_entity_id           convenience copy of the entity whose turn
                                 it was; None if turn order is empty
      timestamp                  ISO 8601 UTC for human readability
      label                      kind-specific: command snapshots store the
                                 command line that triggered them, manual
                                 snapshots store the user-provided name;
                                 round/turn snapshots leave this empty
      state                      Match.to_dict(include_history=False)

    A snapshot persisted by the storage layer (storage.py) can drop its
    state from memory: `path` names the file holding it, and `state` reads
    it back on first use. A snapshot loaded at startup starts that way, so
    a long history costs disk, not memory, until an undo needs it.
    """
    kind: str
    sequence: int
    turn_index_at_snapshot: int
    round_at_snapshot: int
    active_index: int
    active_entity_id: Optional[str]
    timestamp: str
    label: str
    _state: Optional[Dict[str, Any]] = None
    path: Optional[str] = None
    # Who ran the command a command snapshot comes before ("" for the
    # automatic and manual ones, and for snapshots saved before it existed).
    by: str = ""
    # A diff snapshot (see state_diff): the sequence of the autosave before
    # it, which its diff applies to; None = a full state (a keyframe).
    base: Optional[int] = None
    # Diffs since the last keyframe (KEYFRAME_EVERY).
    depth: int = 0
    _diff: Optional[Dict[str, Any]] = field(default=None, repr=False,
                                            compare=False)
    # The history's lookup of a snapshot by sequence (the base of a diff).
    _resolve: Optional[Callable[[int], Optional["Snapshot"]]] = field(
        default=None, repr=False, compare=False)

    def _read(self) -> None:
        if self.path is None:
            raise HistoryError(
                f"Snapshot {self.sequence} has no saved state.")
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            if self.base is None:
                self._state = raw["state"]
            else:
                self._diff = raw["diff"]
                if not isinstance(self._diff, dict):
                    raise TypeError("diff")
        except (OSError, ValueError, KeyError, TypeError) as ex:
            raise HistoryError(
                f"Snapshot {self.sequence} couldn't be read from disk "
                f"({type(ex).__name__}).")

    @property
    def diff(self) -> Dict[str, Any]:
        """A diff snapshot's diff against its base."""
        if self._diff is None:
            self._read()
        return self._diff or {}

    @property
    def state(self) -> Dict[str, Any]:
        """The full match state (a diff snapshot's base with its diff
        applied; cached until release_state). SHARED with the snapshots
        around it: nothing may change it."""
        if self._state is None:
            if self.base is None:
                self._read()
            else:
                chain: List[Snapshot] = []
                cur: Snapshot = self
                while cur._state is None and cur.base is not None:
                    chain.append(cur)
                    nxt = cur._resolve(cur.base) if cur._resolve else None
                    if nxt is None:
                        raise HistoryError(
                            f"Snapshot {self.sequence} builds on snapshot "
                            f"{cur.base}, which is gone.")
                    cur = nxt
                st = cur.state
                for snap in reversed(chain):
                    st = apply_state_diff(st, snap.diff)
                    snap._state = st
        return self._state

    def release_state(self) -> None:
        """Drop the in-memory state (and diff) of a snapshot whose file is
        written."""
        if self.path is not None:
            self._state = None
            self._diff = None

    def meta(self) -> Dict[str, Any]:
        """Everything but the state (the storage index entry)."""
        d = {
            "kind": self.kind,
            "sequence": self.sequence,
            "turn_index_at_snapshot": self.turn_index_at_snapshot,
            "round_at_snapshot": self.round_at_snapshot,
            "active_index": self.active_index,
            "active_entity_id": self.active_entity_id,
            "timestamp": self.timestamp,
            "label": self.label,
            "by": self.by,
        }
        if self.base is not None:
            d["base"] = self.base
            d["depth"] = self.depth
        return d

    @staticmethod
    def from_meta(d: Dict[str, Any], path: str) -> "Snapshot":
        """A snapshot whose state stays on disk until read."""
        base = d.get("base")
        return Snapshot(
            kind=d["kind"],
            sequence=int(d["sequence"]),
            turn_index_at_snapshot=int(d.get("turn_index_at_snapshot", 0)),
            round_at_snapshot=int(d["round_at_snapshot"]),
            active_index=int(d["active_index"]),
            active_entity_id=d.get("active_entity_id"),
            timestamp=d.get("timestamp", ""),
            label=d.get("label", ""),
            path=path,
            by=str(d.get("by") or ""),
            base=int(base) if isinstance(base, int)
            and not isinstance(base, bool) else None,
            depth=int(d.get("depth") or 0) if isinstance(base, int) else 0,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "sequence": self.sequence,
            "turn_index_at_snapshot": self.turn_index_at_snapshot,
            "round_at_snapshot": self.round_at_snapshot,
            "active_index": self.active_index,
            "active_entity_id": self.active_entity_id,
            "timestamp": self.timestamp,
            "label": self.label,
            "by": self.by,
            "state": self.state,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Snapshot":
        return Snapshot(
            kind=d["kind"],
            sequence=int(d["sequence"]),
            turn_index_at_snapshot=int(d.get("turn_index_at_snapshot", 0)),
            round_at_snapshot=int(d["round_at_snapshot"]),
            active_index=int(d["active_index"]),
            active_entity_id=d.get("active_entity_id"),
            timestamp=d.get("timestamp", ""),
            label=d.get("label", ""),
            _state=d["state"],
            by=str(d.get("by") or ""),
        )

    def short_summary(self) -> str:
        """One-line description used in !history list output."""
        who = f" ({self.active_entity_id}'s turn)" if self.active_entity_id else ""
        if self.kind == "round":
            return f"round-start of round {self.round_at_snapshot}"
        if self.kind == "turn":
            return f"turn-start of round {self.round_at_snapshot}{who}"
        if self.kind == "command":
            return (f"before {formula_label(self.label)!r} "
                    f"(round {self.round_at_snapshot}{who})")
        return f"manual '{self.label}' (round {self.round_at_snapshot}{who})"


@dataclass
class MatchHistory:
    """The save store for one match.

    Each Match owns one MatchHistory instance via Match.history. The
    history is mutated by:
      - Match.next_turn() → record_round / record_turn
      - the command dispatcher → record_command (with dedup against
        no-op commands)
      - !history save / restore / delete / undo

    All public methods that mutate state may raise HistoryError on bad
    input; the !history command catches and surfaces those.
    """
    round_saves: List[Snapshot] = field(default_factory=list)
    turn_saves: List[Snapshot] = field(default_factory=list)
    command_saves: List[Snapshot] = field(default_factory=list)
    manual_saves: Dict[str, Snapshot] = field(default_factory=dict)

    # Monotonic sequence counter. Bumped each time a snapshot is created.
    # Never reset on restore — old sequences vanish via pruning, new
    # snapshots simply continue the count. This is what lets us prune
    # "everything after the restore point" with a single comparison.
    _seq: int = 0

    # Count of total individual turns elapsed. Bumped each time
    # record_turn() runs. Used as the basis for "keep last N turns
    # worth of command snapshots" pruning — a command snapshot is
    # retained iff its turn_index_at_snapshot >= current - retention + 1.
    _turn_index: int = 0

    # ---- snapshot creation primitives ----

    # Full autosaves pruned from the lists above that later diffs still
    # build on (the oldest of a chain): kept, unlisted, until no diff needs
    # them, so a prune at the head of a chain writes no new full state.
    hidden_bases: List[Snapshot] = field(default_factory=list)

    # The newest autosave's state, (sequence, state), kept in memory so the
    # next autosave's diff needs no read back (runtime-only).
    _tail: Optional[Tuple[int, Dict[str, Any]]] = field(
        default=None, repr=False, compare=False)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def autosaves(self) -> List[Snapshot]:
        """The round, turn and command autosaves by sequence: the chain
        each diff snapshot builds on."""
        return sorted(self.round_saves + self.turn_saves + self.command_saves
                      + self.hidden_bases, key=lambda x: x.sequence)

    def _by_seq(self, seq: int) -> Optional[Snapshot]:
        for lst in (self.command_saves, self.turn_saves, self.round_saves,
                    self.hidden_bases):
            for x in lst:
                if x.sequence == seq:
                    return x
        return None

    def _adopt(self, snap: Snapshot) -> Snapshot:
        snap._resolve = self._by_seq
        return snap

    def _make_snapshot(
        self, match: "Match", kind: str, label: str = "",
        state: Optional[Dict[str, Any]] = None,
    ) -> Snapshot:
        """Build a Snapshot from `match`'s current state.

        `state` may be provided when the caller already serialized the
        match (the command dispatcher captures pre-state before dispatch,
        then passes it here post-dispatch if the command turned out to
        be mutating). Avoids serializing the same Match dict twice.

        A turn or command autosave holds a diff against the newest
        autosave (`base`); a round autosave, the first one, one every
        KEYFRAME_EVERY and a manual save are full states."""
        if state is None:
            state = match.to_dict(include_history=False)
        snap = self._adopt(Snapshot(
            kind=kind,
            sequence=self._next_seq(),
            turn_index_at_snapshot=self._turn_index,
            round_at_snapshot=match.round_number,
            active_index=match.active_index,
            active_entity_id=match.current_entity_id(),
            timestamp=_now_iso(),
            label=label,
            _state=state,
        ))
        if kind == "manual":
            return snap
        chain = self.autosaves()
        prev = chain[-1] if chain else None
        if kind != "round" and prev is not None \
                and prev.depth + 1 < KEYFRAME_EVERY:
            try:
                tail = self._tail
                pstate = (tail[1] if tail is not None
                          and tail[0] == prev.sequence else prev.state)
                snap._diff = state_diff(pstate, state)
                snap.base = prev.sequence
                snap.depth = prev.depth + 1
            except HistoryError:
                pass        # the one before is unreadable: a full state
        self._tail = (snap.sequence, state)
        return snap

    # ---- keeping the chain whole -------------------------------------

    def _relink(self, before: List[Snapshot]) -> None:
        """After autosaves were removed (`before`: the autosaves until then,
        by sequence): every diff snapshot whose base went builds on the
        autosave now before it, its diff composed with the removed ones'
        (merge on prune), or becomes a full state when a removed keyframe
        lay between or nothing is left before it. A snapshot that can't be
        rebuilt (an unreadable file) goes, with the ones building on it."""
        old = {x.sequence: x for x in before}

        def state_via(x: Snapshot) -> Dict[str, Any]:
            chain = []
            cur = x
            while cur._state is None and cur.base is not None:
                chain.append(cur)
                cur = old.get(cur.base) or self._by_seq(cur.base)
                if cur is None:
                    raise HistoryError("gone")
            st = cur.state
            for y in reversed(chain):
                st = apply_state_diff(st, y.diff)
            return st

        lost: List[int] = []
        prev: Optional[Snapshot] = None
        for x in self.autosaves():
            want = prev.sequence if prev is not None else None
            if x.sequence in lost or (x.base is not None
                                      and x.base in lost):
                lost.append(x.sequence)
                continue
            if x.base is None or x.base == want:
                prev = x
                continue
            try:
                diffs = []
                cur: Optional[Snapshot] = x
                keyframe = False
                while cur is not None and (want is None
                                           or cur.sequence > want):
                    if cur.base is None:
                        keyframe = True
                        break
                    diffs.append(cur.diff)
                    cur = old.get(cur.base)
                if keyframe and want is None and cur is not None \
                        and cur.sequence in old \
                        and cur not in self.hidden_bases:
                    # The chain's full state was pruned: kept unlisted as
                    # its base (hidden_bases), the diffs between merged.
                    self.hidden_bases.append(cur)
                    self.hidden_bases.sort(key=lambda h: h.sequence)
                    new = replace(x, base=cur.sequence, depth=len(diffs),
                                  _diff=compose_state_diffs(
                                      list(reversed(diffs))),
                                  _state=x._state)
                elif keyframe or want is None or cur is None:
                    st = state_via(x)
                    if want is None:
                        new = replace(x, base=None, depth=0, _diff=None,
                                      _state=st)
                    else:
                        new = replace(x, base=want, depth=prev.depth + 1,
                                      _diff=state_diff(state_via(prev), st),
                                      _state=st)
                else:
                    new = replace(x, base=want, depth=prev.depth + 1,
                                  _diff=compose_state_diffs(
                                      list(reversed(diffs))),
                                  _state=x._state)
            except HistoryError:
                lost.append(x.sequence)
                continue
            self._swap(x, new)
            prev = new
        if lost:
            self.drop_sequences(lost, relink=False)
            print(f"⚠️ undo snapshot(s) {', '.join(map(str, lost))} "
                  f"couldn't be rebuilt; dropped from the undo history.")
        # A hidden base no diff builds on any more goes.
        chain = self.autosaves()
        needed = {x.base for x in chain if x.base is not None}
        self.hidden_bases = [h for h in self.hidden_bases
                             if h.sequence in needed]

    def _swap(self, old: Snapshot, new: Snapshot) -> None:
        for lst in (self.round_saves, self.turn_saves, self.command_saves,
                    self.hidden_bases):
            for i, x in enumerate(lst):
                if x is old:
                    lst[i] = new
                    return

    # ---- recording (called by Match.next_turn and the dispatcher) ----

    def record_round(self, match: "Match") -> Snapshot:
        """Snapshot taken at the start of a round, after on_round_start
        hooks have already fired. Called from Match.next_turn()."""
        s = self._make_snapshot(match, "round")
        self.round_saves.append(s)
        self._prune_rounds(match)
        return s

    def record_turn(self, match: "Match") -> Snapshot:
        """Snapshot taken at the start of an entity's turn, after
        on_turn_start hooks have fired. Bumps the turn counter, which
        cascades into command-snapshot pruning (commands are scoped to
        their enclosing turn for retention purposes)."""
        self._turn_index += 1
        s = self._make_snapshot(match, "turn")
        self.turn_saves.append(s)
        self._prune_turns(match)
        # A new turn boundary also re-prunes commands: the previous
        # turn's commands may now be outside the retention window.
        self._prune_commands(match)
        return s

    def record_command(
        self, match: "Match", cmd_label: str,
        pre_state: Optional[Dict[str, Any]] = None, by: str = "",
    ) -> Snapshot:
        """Snapshot taken before a mutating command. The dispatcher
        captures pre-state up front and only calls this if the command
        actually changed the match — read-only commands and no-op
        mutations don't accumulate snapshots."""
        s = self._make_snapshot(match, "command", label=cmd_label, state=pre_state)
        s.by = by
        self.command_saves.append(s)
        self._prune_commands(match)
        return s

    def save_manual(self, match: "Match", name: str) -> Snapshot:
        """Create or overwrite a named manual save. Overwriting is
        intentional — users can iterate on a single bookmark name."""
        if not name:
            raise HistoryError("Manual save name cannot be empty.")
        s = self._make_snapshot(match, "manual", label=name)
        self.manual_saves[name] = s
        return s

    def delete_manual(self, name: str) -> None:
        if name not in self.manual_saves:
            raise HistoryError(f"No manual save named '{name}'.")
        del self.manual_saves[name]

    # ---- retrieval ----

    def get_turn_nth_back(self, n: int) -> Snapshot:
        """Return the snapshot from N turns ago. N=1 is the most recent
        turn snapshot (= state at start of current turn). Raises if N is
        out of range or non-positive."""
        if n < 1:
            raise HistoryError(f"N must be >= 1 (got {n}).")
        if n > len(self.turn_saves):
            raise HistoryError(
                f"Cannot undo {n} turn(s) — only {len(self.turn_saves)} turn "
                f"snapshot(s) retained. Adjust autosave_turn_retention_rounds "
                f"to keep more, or use a manual save."
            )
        return self.turn_saves[-n]

    def get_round_nth_back(self, n: int) -> Snapshot:
        """Return the snapshot from N rounds ago. Same indexing as turn."""
        if n < 1:
            raise HistoryError(f"N must be >= 1 (got {n}).")
        if n > len(self.round_saves):
            raise HistoryError(
                f"Cannot undo {n} round(s) — only {len(self.round_saves)} "
                f"round snapshot(s) retained. Adjust autosave_round_retention "
                f"to keep more, or use a manual save."
            )
        return self.round_saves[-n]

    def get_command_nth_back(self, n: int) -> Snapshot:
        if n < 1:
            raise HistoryError(f"N must be >= 1 (got {n}).")
        if n > len(self.command_saves):
            raise HistoryError(
                f"Cannot undo {n} command(s) — only "
                f"{len(self.command_saves)} command snapshot(s) retained. "
                f"Adjust autosave_command_retention_turns / "
                f"autosave_command_retention_max to keep more."
            )
        return self.command_saves[-n]

    def get_round_with_number(self, round_number: int) -> Snapshot:
        """Find the round snapshot for a specific round_number. Used by
        `!history undo to round X`."""
        for s in self.round_saves:
            if s.round_at_snapshot == round_number:
                return s
        available = sorted({s.round_at_snapshot for s in self.round_saves})
        raise HistoryError(
            f"No round-start snapshot for round {round_number}. "
            f"Available rounds: {available}"
        )

    def get_manual(self, name: str) -> Snapshot:
        if name not in self.manual_saves:
            raise HistoryError(f"No manual save named '{name}'.")
        return self.manual_saves[name]

    # ---- post-restore pruning ----

    def truncate_after(self, snapshot: Snapshot) -> int:
        """Drop every autosave with sequence > snapshot.sequence.

        Called after restoring a snapshot. The newer autosaves are now
        a stale "future" — they describe a timeline that didn't happen.
        Manual saves are exempt (they're explicit bookmarks; user owns
        their lifecycle).

        Returns the count of removed snapshots, for the user-facing
        ack message.
        """
        cut = snapshot.sequence
        before = (len(self.round_saves) + len(self.turn_saves)
                  + len(self.command_saves))
        self.round_saves = [s for s in self.round_saves if s.sequence <= cut]
        self.turn_saves = [s for s in self.turn_saves if s.sequence <= cut]
        # A command snapshot is the state BEFORE its command ran, so once it
        # is restored it IS the current state — keep it and the next
        # `undo command 1` would "restore" it again (a silent no-op that
        # still reports "Undid 1 command"), so repeated single undos never
        # stepped further back. Drop the restored command snapshot itself.
        # (Round/turn snapshots mark the START of a round/turn — `undo turn
        # 1` = back to the start of the current turn — so they stay.)
        chain = self.autosaves()
        if snapshot.kind == "command":
            self.command_saves = [s for s in self.command_saves
                                  if s.sequence < cut]
        else:
            self.command_saves = [s for s in self.command_saves
                                  if s.sequence <= cut]
        # Restore the turn-counter to what it was at snapshot time so
        # subsequent commands prune correctly against the new "current"
        # turn position.
        self._turn_index = snapshot.turn_index_at_snapshot
        after = (len(self.round_saves) + len(self.turn_saves)
                 + len(self.command_saves))
        self._pruned(chain)
        return before - after

    # ---- retention pruning ----

    def _pruned(self, before: List[Snapshot]) -> None:
        if len(before) != (len(self.round_saves) + len(self.turn_saves)
                           + len(self.command_saves)
                           + len(self.hidden_bases)):
            self._relink(before)

    def _prune_rounds(self, match: "Match") -> None:
        before = self.autosaves()
        self._prune_rounds_(match)
        self._pruned(before)

    def _prune_turns(self, match: "Match") -> None:
        before = self.autosaves()
        self._prune_turns_(match)
        self._pruned(before)

    def _prune_commands(self, match: "Match") -> None:
        before = self.autosaves()
        self._prune_commands_(match)
        self._pruned(before)

    def _prune_rounds_(self, match: "Match") -> None:
        limit = int(match.rules.get("autosave_round_retention", -1))
        if limit < 0:  # unlimited
            return
        if limit == 0:
            self.round_saves.clear()
            return
        excess = len(self.round_saves) - limit
        if excess > 0:
            del self.round_saves[:excess]

    def _prune_turns_(self, match: "Match") -> None:
        # ATB: rounds are disabled, so match.round_number never advances and a
        # round-keyed window would retain EVERY turn snapshot for the whole
        # session (each one a full match state). Prune by the turn counter
        # instead — the only clock that advances under ATB.
        if match.rules.get("atb_enabled"):
            turns = int(match.rules.get("autosave_turn_retention_turns", 60))
            if turns < 0:
                return
            if turns == 0:
                self.turn_saves.clear()
                return
            threshold = self._turn_index - turns + 1
            self.turn_saves = [s for s in self.turn_saves
                               if s.turn_index_at_snapshot >= threshold]
            return
        rounds = int(match.rules.get("autosave_turn_retention_rounds", 10))
        if rounds < 0:
            return
        if rounds == 0:
            self.turn_saves.clear()
            return
        threshold = match.round_number - rounds + 1
        self.turn_saves = [s for s in self.turn_saves
                           if s.round_at_snapshot >= threshold]

    def _prune_commands_(self, match: "Match") -> None:
        turns = int(match.rules.get("autosave_command_retention_turns", 10))
        if turns == 0:
            self.command_saves.clear()
            return
        if turns > 0:
            threshold = self._turn_index - turns + 1
            self.command_saves = [s for s in self.command_saves
                                  if s.turn_index_at_snapshot >= threshold]
        # Count cap on top of the turn window: the window only prunes when a
        # turn advances, so without this a long setup phase (no `!turn next`
        # yet) kept one full match snapshot per command, unbounded.
        cap = int(match.rules.get("autosave_command_retention_max", 500))
        if cap >= 0 and len(self.command_saves) > cap:
            del self.command_saves[:len(self.command_saves) - cap]

    def all_snapshots(self) -> List[Snapshot]:
        """Every snapshot this history holds (autosaves and manual saves)."""
        return (list(self.round_saves) + list(self.turn_saves)
                + list(self.command_saves) + list(self.manual_saves.values())
                + list(self.hidden_bases))

    def mark(self) -> Tuple[Any, ...]:
        """The history as it stands, for rewind(): a rollback window
        (`!dry`, `!batch strict`, an action) takes one before it runs."""
        return (self._turn_index, list(self.round_saves),
                list(self.turn_saves), list(self.command_saves),
                dict(self.manual_saves), list(self.hidden_bases))

    def rewind(self, mark: Tuple[Any, ...]) -> None:
        """Back to `mark` (mark()): the snapshots recorded during work that
        was rolled back go (a turn autosave of a turn that never happened
        would be the next `!undo turn`), the ones pruned meanwhile return,
        and the turn counter goes back. The sequence counter keeps counting,
        so no number is reused."""
        (self._turn_index, self.round_saves, self.turn_saves,
         self.command_saves, self.manual_saves, self.hidden_bases) = (
            mark[0], list(mark[1]), list(mark[2]), list(mark[3]),
            dict(mark[4]), list(mark[5]))

    def drop_sequences(self, seqs, relink: bool = True) -> None:
        """Remove the autosaves with these sequence numbers (the storage
        layer cutting old autosaves to fit a storage limit). Manual saves
        are never dropped this way. The diffs that built on them are
        merged into the next (_relink)."""
        seqs = set(seqs)
        before = self.autosaves()
        self.round_saves = [s for s in self.round_saves if s.sequence not in seqs]
        self.turn_saves = [s for s in self.turn_saves if s.sequence not in seqs]
        self.command_saves = [s for s in self.command_saves
                              if s.sequence not in seqs]
        if relink:
            self._pruned(before)

    def discard(self, seqs) -> None:
        """Remove every snapshot with these sequence numbers, manual saves
        included (storage dropping snapshots whose state is lost: a
        missing or unreadable file), and the diff snapshots building on
        them (their changes can't be rebuilt)."""
        seqs = set(seqs)
        for x in self.autosaves():
            if x.base is not None and x.base in seqs:
                seqs.add(x.sequence)
        self.drop_sequences(seqs, relink=False)
        self.hidden_bases = [h for h in self.hidden_bases
                             if h.sequence not in seqs]
        for name in [n for n, s in self.manual_saves.items()
                     if s.sequence in seqs]:
            del self.manual_saves[name]

    def index(self) -> Dict[str, Any]:
        """The history's shape without any state: what storage writes to
        the history index file (each snapshot's state is its own file)."""
        return {
            "round_saves": [s.meta() for s in self.round_saves],
            "turn_saves": [s.meta() for s in self.turn_saves],
            "command_saves": [s.meta() for s in self.command_saves],
            "manual_saves": {n: s.meta() for n, s in self.manual_saves.items()},
            "hidden_bases": [s.meta() for s in self.hidden_bases],
            "_seq": self._seq,
            "_turn_index": self._turn_index,
        }

    @staticmethod
    def from_index(d: Dict[str, Any], path_of) -> "MatchHistory":
        """Rebuild a history from its index; `path_of(sequence)` names the
        file holding each snapshot's state (read on first use)."""
        h = MatchHistory()
        def mk(m):
            return Snapshot.from_meta(m, path_of(int(m["sequence"])))
        h.round_saves = [mk(m) for m in d.get("round_saves", [])]
        h.turn_saves = [mk(m) for m in d.get("turn_saves", [])]
        h.command_saves = [mk(m) for m in d.get("command_saves", [])]
        h.manual_saves = {n: mk(m) for n, m in d.get("manual_saves", {}).items()}
        h.hidden_bases = [mk(m) for m in d.get("hidden_bases", [])]
        for x in h.all_snapshots():
            h._adopt(x)
        h._seq = int(d.get("_seq", 0))
        h._turn_index = int(d.get("_turn_index", 0))
        return h

    # ---- serialization ----

    def to_dict(self) -> Dict[str, Any]:
        return {
            "round_saves": [s.to_dict() for s in self.round_saves],
            "turn_saves": [s.to_dict() for s in self.turn_saves],
            "command_saves": [s.to_dict() for s in self.command_saves],
            "manual_saves": {n: s.to_dict() for n, s in self.manual_saves.items()},
            "_seq": self._seq,
            "_turn_index": self._turn_index,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "MatchHistory":
        h = MatchHistory()
        h.round_saves = [Snapshot.from_dict(s) for s in d.get("round_saves", [])]
        h.turn_saves = [Snapshot.from_dict(s) for s in d.get("turn_saves", [])]
        h.command_saves = [Snapshot.from_dict(s) for s in d.get("command_saves", [])]
        h.manual_saves = {
            n: Snapshot.from_dict(s) for n, s in d.get("manual_saves", {}).items()
        }
        for x in h.all_snapshots():
            h._adopt(x)
        h._seq = int(d.get("_seq", 0))
        h._turn_index = int(d.get("_turn_index", 0))
        return h
