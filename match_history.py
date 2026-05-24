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

- Retention is rule-driven. Three independent dials live on Match.rules:
    autosave_round_retention          (default -1 / unlimited)
    autosave_turn_retention_rounds    (default 3)
    autosave_command_retention_turns  (default 3)
  -1 means unlimited, 0 disables that kind entirely, N>0 caps to last N.

- Manual saves never auto-prune. They live until explicitly deleted or
  the enclosing match is removed.

- This whole structure travels with the Match in memory but is excluded
  from Match.to_dict() by default to keep !store save sizes reasonable.
  Opt in with to_dict(include_history=True) when you do want the
  autosave history persisted (e.g. for a long-term campaign backup).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING
import datetime

# Import VTTError eagerly so HistoryError can subclass it. This works
# because logic.py imports us AFTER its exception classes are defined
# (see the late import comment in logic.py).
from logic import VTTError

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
    """
    kind: str
    sequence: int
    turn_index_at_snapshot: int
    round_at_snapshot: int
    active_index: int
    active_entity_id: Optional[str]
    timestamp: str
    label: str
    state: Dict[str, Any]

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
            state=d["state"],
        )

    def short_summary(self) -> str:
        """One-line description used in !history list output."""
        who = f" ({self.active_entity_id}'s turn)" if self.active_entity_id else ""
        if self.kind == "round":
            return f"round-start of round {self.round_at_snapshot}"
        if self.kind == "turn":
            return f"turn-start of round {self.round_at_snapshot}{who}"
        if self.kind == "command":
            return f"before {self.label!r} (round {self.round_at_snapshot}{who})"
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

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _make_snapshot(
        self, match: "Match", kind: str, label: str = "",
        state: Optional[Dict[str, Any]] = None,
    ) -> Snapshot:
        """Build a Snapshot from `match`'s current state.

        `state` may be provided when the caller already serialized the
        match (the command dispatcher captures pre-state before dispatch,
        then passes it here post-dispatch if the command turned out to
        be mutating). Avoids serializing the same Match dict twice.
        """
        if state is None:
            state = match.to_dict(include_history=False)
        return Snapshot(
            kind=kind,
            sequence=self._next_seq(),
            turn_index_at_snapshot=self._turn_index,
            round_at_snapshot=match.round_number,
            active_index=match.active_index,
            active_entity_id=match.current_entity_id(),
            timestamp=_now_iso(),
            label=label,
            state=state,
        )

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
        pre_state: Optional[Dict[str, Any]] = None,
    ) -> Snapshot:
        """Snapshot taken before a mutating command. The dispatcher
        captures pre-state up front and only calls this if the command
        actually changed the match — read-only commands and no-op
        mutations don't accumulate snapshots."""
        s = self._make_snapshot(match, "command", label=cmd_label, state=pre_state)
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
                f"Adjust autosave_command_retention_turns to keep more."
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
        self.command_saves = [s for s in self.command_saves if s.sequence <= cut]
        # Restore the turn-counter to what it was at snapshot time so
        # subsequent commands prune correctly against the new "current"
        # turn position.
        self._turn_index = snapshot.turn_index_at_snapshot
        after = (len(self.round_saves) + len(self.turn_saves)
                 + len(self.command_saves))
        return before - after

    # ---- retention pruning ----

    def _prune_rounds(self, match: "Match") -> None:
        limit = int(match.rules.get("autosave_round_retention", -1))
        if limit < 0:  # unlimited
            return
        if limit == 0:
            self.round_saves.clear()
            return
        excess = len(self.round_saves) - limit
        if excess > 0:
            del self.round_saves[:excess]

    def _prune_turns(self, match: "Match") -> None:
        rounds = int(match.rules.get("autosave_turn_retention_rounds", 3))
        if rounds < 0:
            return
        if rounds == 0:
            self.turn_saves.clear()
            return
        threshold = match.round_number - rounds + 1
        self.turn_saves = [s for s in self.turn_saves
                           if s.round_at_snapshot >= threshold]

    def _prune_commands(self, match: "Match") -> None:
        turns = int(match.rules.get("autosave_command_retention_turns", 3))
        if turns < 0:
            return
        if turns == 0:
            self.command_saves.clear()
            return
        threshold = self._turn_index - turns + 1
        self.command_saves = [s for s in self.command_saves
                              if s.turn_index_at_snapshot >= threshold]

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
        h._seq = int(d.get("_seq", 0))
        h._turn_index = int(d.get("_turn_index", 0))
        return h
