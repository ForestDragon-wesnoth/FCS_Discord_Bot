"""The per-match command journal (`!match journal`, `!match replay`).

A journal is a list of SEGMENTS, oldest first, at most two. Each starts from
a stored state (`start`: the match state without its rules, the systems of
its chain, the server's templates, when and why it started) and lists the
ENTRIES after it, numbered on across segments (`n`):

  cmd       a command that changed the match: its tokens as typed, who ran
            it (`by`, `uid`), the channel, when, the RNG [seed, position
            before, position after], a hash of the state after it and the
            prefab library reads it made (`prefabs`, prefab_reads)
  state     a stored state, for a change a command can't replay from the
            match alone: an undo or history restore, a `!run` file, a change
            made from a channel showing another match
  systems   the match's system chain after a server-wide edit
  template  a server template saved, replaced, renamed or deleted

When the newest segment holds command_journal_limit entries, the next one
starts a new segment from the state then, and the segment before it is
dropped: a journal holds between the limit and twice the limit entries, and
an export always starts from a real state.

The journal is runtime state on the Match (`Match.journal`): it isn't in
to_dict, undo snapshots or match files. storage.py writes it under
matches/<id>/journal/ (a header file and an append-only entries file per
segment)."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterator, List, Optional

from match_history import _now_iso

JOURNAL_FORMAT = "fcs-journal"
JOURNAL_VERSION = 1

# Match fields an undo leaves alone (vtt_commands._NOT_UNDO_STATE): a change
# to them alone is no journal entry or undo step, and the state hash leaves
# them out. channel_views / channel_maps are each channel's camera and
# `!map use` choice: display state (a player panning took undo steps).
UNDO_IGNORED = ("paused", "rules", "rng_seed", "rng_pos", "channel_views",
                "channel_maps")
# The journal records a `!map use` all the same: a later `map=view` or a
# strict edit's channel map reads it, so a replay needs it.
JOURNAL_IGNORED = tuple(k for k in UNDO_IGNORED if k != "channel_maps")

# Segments kept: the newest and the one before it.
KEPT_SEGMENTS = 2


def journal_limit(m: Any) -> int:
    """The match's command_journal_limit, with -1 (and anything past the
    owner's ceiling) read as the ceiling; 0 = no journal."""
    from logic import rule_max
    hi = rule_max("command_journal_limit")
    try:
        v = int(m.rules.get("command_journal_limit", 5000))
    except (TypeError, ValueError):
        v = 5000
    if hi is None:
        return v if v >= 0 else 100_000
    return hi if v < 0 or v > hi else v


def journal_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """A match state (Match.to_dict) as a journal stores it: without its
    rules, which are rebuilt from the systems."""
    return {k: v for k, v in state.items() if k != "rules"}


def state_hash(state: Dict[str, Any]) -> str:
    """A short hash of what an undo restores in `state` (bar the match id:
    a replay may rename it), to tell where a replay stops matching the
    recording."""
    body = {k: v for k, v in state.items()
            if k not in UNDO_IGNORED and k != "id"}
    text = json.dumps(body, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def effective_seed(m: Any) -> str:
    return str(m.rules.get("random_seed", "") or "") or m.rng_seed


class Journal:
    def __init__(self) -> None:
        self.segments: List[Dict[str, Any]] = []
        self.next_n = 1

    def entries(self) -> Iterator[Dict[str, Any]]:
        for seg in self.segments:
            yield from seg["entries"]

    def count(self) -> int:
        return sum(len(seg["entries"]) for seg in self.segments)

    def add(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        entry["n"] = self.next_n
        self.next_n += 1
        self.segments[-1]["entries"].append(entry)
        return entry

    def last_state_hash(self) -> Optional[str]:
        """The state hash the journal last recorded: its newest entry that
        carries one, else its newest segment's starting state."""
        for seg in reversed(self.segments):
            for e in reversed(seg["entries"]):
                if e.get("hash"):
                    return e["hash"]
            st = (seg.get("start") or {}).get("state")
            if isinstance(st, dict):
                return state_hash(st)
        return None

    def start_segment(self, header: Dict[str, Any]) -> None:
        header["no"] = (self.segments[-1]["no"] + 1) if self.segments else 1
        header["first_n"] = self.next_n
        self.segments.append({"no": header["no"], "start": header,
                              "entries": []})
        del self.segments[:-KEPT_SEGMENTS]


def chain_systems(mgr: Any, m: Any) -> Dict[str, Any]:
    """The systems the match reads its rules from, root first."""
    s = mgr.systems.get(m.system_name)
    if s is None:
        return {}
    return {x.name: x.to_dict() for x in mgr.system_chain(s)}


def start_header(mgr: Any, m: Any, reason: str,
                 state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    import copy
    if state is None:
        state = m.to_dict(include_history=False)
    return {"at": _now_iso(), "reason": reason, "match": m.id,
            "system_name": m.system_name,
            "state": journal_state(copy.deepcopy(state)),
            "systems": chain_systems(mgr, m),
            "templates": copy.deepcopy(mgr.templates)}


def ensure_journal(mgr: Any, m: Any, reason: str = "started",
                   state: Optional[Dict[str, Any]] = None
                   ) -> Optional[Journal]:
    """The match's journal, started now (from `state`, else its current
    state) when it has none; None (and any journal dropped) when the rule
    turns it off."""
    if journal_limit(m) == 0:
        m.journal = None
        return None
    if m.journal is None:
        j = Journal()
        j.start_segment(start_header(mgr, m, reason, state))
        m.journal = j
    return m.journal


def maybe_roll(mgr: Any, m: Any) -> None:
    """Start a new segment once the newest holds the limit's worth."""
    j = m.journal
    if j is None:
        return
    lim = journal_limit(m)
    if lim and len(j.segments[-1]["entries"]) >= lim:
        j.start_segment(start_header(mgr, m, "rollover"))


def bundle(mgr: Any, m: Any) -> Dict[str, Any]:
    """Everything `!match replay` needs, as one JSON value."""
    import copy
    j = m.journal
    return {"format": JOURNAL_FORMAT, "version": JOURNAL_VERSION,
            "match": m.id, "server": mgr.guild_key,
            "exported_at": _now_iso(),
            "segments": copy.deepcopy(j.segments) if j else []}


def _short(tokens: Any) -> str:
    text = " ".join(map(str, tokens or [])).replace("\n", "\\n")
    return text if len(text) <= 100 else text[:97] + "..."


def entry_text(e: Dict[str, Any]) -> str:
    """One entry as a line of `!match journal`."""
    kind = e.get("kind")
    by = f" by {e['by']}" if e.get("by") else ""
    head = f"#{e.get('n')} {e.get('at', '?')}{by}: "
    if kind == "cmd":
        return head + "`!" + _short(e.get("cmd")) + "`"
    if kind == "state":
        return head + "stored state after `!" + _short(e.get("cmd")) + "`"
    if kind == "systems":
        return head + "systems " + ", ".join(
            f"`{n}`" for n in (e.get("systems") or {})) + " changed"
    if kind == "template":
        what = e.get("what") or "changed"
        return head + f"template `{e.get('name')}` {what}"
    return head + str(kind)


def prefab_hash(pf: Any) -> str:
    text = json.dumps(pf, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def prefab_reads(j: Journal, reads: Dict[str, Any]) -> Dict[str, Any]:
    """A command's prefab library reads (logic.PREFAB_READS) as its `cmd`
    entry stores them: `names` the name list it read, `data` per prefab
    name None (none in the library) or {hash, data}. A prefab the
    newest segment already holds is stored as its hash alone, so a
    passive stamping one prefab every turn stores it once per segment."""
    import copy
    held = {d["hash"] for e in j.segments[-1]["entries"]
            for d in ((e.get("prefabs") or {}).get("data") or {}).values()
            if isinstance(d, dict) and "data" in d}
    out: Dict[str, Any] = {}
    if "names" in reads:
        out["names"] = list(reads["names"])
    data: Dict[str, Any] = {}
    for name, pf in (reads.get("data") or {}).items():
        if pf is None:
            data[name] = None
            continue
        h = prefab_hash(pf)
        data[name] = ({"hash": h} if h in held
                      else {"hash": h, "data": copy.deepcopy(pf)})
        held.add(h)
    if data:
        out["data"] = data
    return out


def replay_prefabs(e: Dict[str, Any], seen: Dict[str, Any]
                   ) -> Optional[Dict[str, Any]]:
    """An entry's recorded prefab reads as a replay reads them
    (logic.PREFAB_REPLAY), resolving hashes through `seen` (hash ->
    prefab, filled as the replay goes); None when it recorded none. A
    hash the replay never saw (a hand-edited export) is left out, so that
    prefab reads the library."""
    rec = e.get("prefabs")
    if not isinstance(rec, dict):
        return None
    out: Dict[str, Any] = {"data": {}}
    if isinstance(rec.get("names"), list):
        out["names"] = [str(n) for n in rec["names"]]
    for name, d in (rec.get("data") or {}).items():
        if d is None:
            out["data"][name] = None
        elif isinstance(d, dict) and isinstance(d.get("data"), dict):
            seen[str(d.get("hash"))] = d["data"]
            out["data"][name] = d["data"]
        elif isinstance(d, dict) and d.get("hash") in seen:
            out["data"][name] = seen[d["hash"]]
    return out
