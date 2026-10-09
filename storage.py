"""storage.py — the bot's data on disk: one folder per server.

Layout under the data root (default `data/` next to the code):

    bot_settings.json           the bot owner's settings (storage limits,
                                rule ceilings)
    .trash/<server>/<stamp>-<scope>/
                                what a `!server wipe` removed, kept 24 h for
                                `!server wipe undo` (counts toward the global
                                limit only)
    <server>/                   one per server (a Discord guild id, or
                                `local` for the CLI / GUI)
        workspace.json          channel pointers, default system, per-channel
                                system defaults
        systems.json            the server's game systems
        matches/<id>.json       one per match (Match.to_dict without history)
        matches/<id>/history/   that match's undo history: index.json (the
                                snapshots' metadata) + <sequence>.json each
        saves/                  `!store save`, `!history export`, `!run` files
        sprites/                the server's own sprites (searched before the
                                shared sprites/ folder)
        templates/<name>.json   saved entity templates (`!template`): written
                                when saved, deleted when deleted, loaded at
                                start — even with match persistence off
        corrupt/                files that failed to load, moved aside

The live state is in memory (logic.Workspaces); this module mirrors it. After
every command (`commit`), each file whose content changed is rewritten — the
match state is serialized and compared with the text last written — and files
of removed matches / snapshots are deleted. Each file write is atomic (a temp
file moved over the target); a crash between two files of one command can
leave them a command apart, which is accepted (no journal).

Storage limits: a server's folder may hold `server_limit` bytes and the whole
data root `global_limit` (bot_settings.json, `!owner limit`). A commit that
would grow past either first cuts the server's oldest persisted autosaves
(matches whose `storage_trim_autosaves` rule is on); if that can't make room,
the command is rolled back — every match, the channel pointers and the systems
go back to what is on disk — and the caller is warned. A commit that doesn't
grow the data is always allowed.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from logic import (GameSystem, ID_RE, Match, MatchManager,
                   RULE_CEILINGS, VTTError, Workspaces, check_template,
                   check_template_name)
from match_history import MatchHistory

MB = 1024 * 1024
DEFAULT_SERVER_LIMIT_MB = 1000
DEFAULT_GLOBAL_LIMIT_MB = 20 * 1024
TRASH_SECONDS = 24 * 3600
TRASH_DIR = ".trash"
SETTINGS_FILE = "bot_settings.json"

def valid_id(name: Any) -> bool:
    return isinstance(name, str) and bool(ID_RE.fullmatch(name))


def _dump(obj: Any) -> str:
    """The text written to disk: compact, keys sorted, so an unchanged state
    serializes to the same text and the comparison finds no change."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _disk_state(d: Dict[str, Any]) -> Dict[str, Any]:
    """A match state as written to disk: without its `rules`, a copy of its
    game system's (5-6 KB a match, and again in every undo snapshot) that is
    rebuilt from the system whenever a match is loaded or restored. Leaving
    it out also keeps a rule change from growing every match file."""
    return {k: v for k, v in d.items() if k != "rules"}


def _write_text(path: str, text: str) -> None:
    """Write `text` to `path` all-or-nothing (temp file + rename)."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, f".{os.path.basename(path)}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _dir_size(path: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            total += _file_size(os.path.join(base, name))
    return total


def _remove_path(path: str) -> None:
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def fmt_bytes(n: float) -> str:
    if n >= MB:
        return f"{n / MB:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{int(n)} B"


class Storage:
    """See the module docstring. One instance serves every workspace; it is
    reached from a manager as `mgr.workspaces.storage`."""

    def __init__(self, root: str, persist: bool = True):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        # persist=False (cli.py / gui.py with match persistence off in
        # local_settings.json): nothing is loaded from or committed to the
        # server folders; the data folder still holds the bot settings and
        # each server's saves/ and sprites/.
        self.persist = persist
        self.settings: Dict[str, Any] = {"limits": {}, "ceilings": {}}
        # What was last written, per server: the text of each file, so a
        # commit rewrites only what changed and a rollback can read it back.
        #   {"workspace": str, "systems": str, "matches": {mid: str},
        #    "index": {mid: str}, "seqs": {mid: set}, "hist": {mid: (seq, turn)}}
        self._written: Dict[str, Dict[str, Any]] = {}
        # Bytes in each top-level folder of the root (a server, or .trash),
        # kept current by the writes this class makes; rescanned by
        # `rescan` (startup, `!owner storage`, files written elsewhere).
        self._usage: Dict[str, int] = {}
        self._last_trash_purge = 0.0
        # storage_trim_autosaves per match as of the last commit (see
        # _trim_candidates).
        self._trim_before: Dict[str, Dict[str, bool]] = {}
        self._load_settings()
        # The owner's rule ceilings are process-wide (logic.RULE_CEILINGS).
        RULE_CEILINGS.clear()
        RULE_CEILINGS.update(self.settings["ceilings"])

    # ---- paths ------------------------------------------------------------
    def server_dir(self, key: str) -> str:
        if not valid_id(key):
            raise VTTError("This server's workspace key can't name a folder.")
        return os.path.join(self.root, key)

    def saves_dir(self, key: str) -> str:
        return os.path.join(self.server_dir(key), "saves")

    def sprites_dir(self, key: str) -> str:
        return os.path.join(self.server_dir(key), "sprites")

    def _match_file(self, key: str, mid: str) -> str:
        return os.path.join(self.server_dir(key), "matches", f"{mid}.json")

    def _history_dir(self, key: str, mid: str) -> str:
        return os.path.join(self.server_dir(key), "matches", mid, "history")

    def _snap_file(self, key: str, mid: str, seq: int) -> str:
        return os.path.join(self._history_dir(key, mid), f"{seq}.json")

    def _trash_root(self) -> str:
        return os.path.join(self.root, TRASH_DIR)

    def templates_dir(self, key: str) -> str:
        return os.path.join(self.server_dir(key), "templates")

    def template_file(self, key: str, name: str) -> str:
        return os.path.join(self.templates_dir(key), f"{name}.json")

    # ---- entity templates -------------------------------------------------
    # Templates are the server's library, outside the match state: each one
    # is written the moment it is saved and removed the moment it is deleted
    # (never through commit / rollback), and they load at start whether or
    # not match persistence is on.
    def write_template(self, mgr: MatchManager, name: str,
                       data: Dict[str, Any]) -> None:
        key = mgr.guild_key
        path = self.template_file(key, name)
        text = json.dumps(data, indent=2, sort_keys=True) + "\n"
        self.check_room(key, path, len(text.encode("utf-8")))
        old = _file_size(path)
        _write_text(path, text)
        self.note_written(key, path, old)

    def delete_template(self, mgr: MatchManager, name: str) -> None:
        key = mgr.guild_key
        path = self.template_file(key, name)
        old = _file_size(path)
        _remove_path(path)
        self._add_usage(key, -old)

    def load_templates(self, mgr: MatchManager) -> List[str]:
        """Read the server's templates/ folder into `mgr.templates`; a file
        that doesn't load is moved to corrupt/."""
        key = mgr.guild_key
        notes: List[str] = []
        mgr.templates = {}
        tdir = self.templates_dir(key)
        if not os.path.isdir(tdir):
            return notes
        for fname in sorted(os.listdir(tdir)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(tdir, fname)
            try:
                name = check_template_name(fname[:-5])
                if name != fname[:-5]:
                    raise ValueError("file name isn't a template name")
                data, _ = check_template(self._read_json(fpath), name)
            except Exception as ex:  # noqa: BLE001 - any bad file is quarantined
                notes.append(self._quarantine(key, fpath, type(ex).__name__))
                continue
            mgr.templates[name] = data
        return notes

    # ---- bot settings -----------------------------------------------------
    def _load_settings(self) -> None:
        path = os.path.join(self.root, SETTINGS_FILE)
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as ex:
            print(f"⚠️ {SETTINGS_FILE} couldn't be read ({ex}); using the "
                  f"default limits.")
            return
        self.settings["limits"] = dict(data.get("limits") or {})
        self.settings["ceilings"] = {
            str(k): int(v) for k, v in (data.get("ceilings") or {}).items()
            if isinstance(v, (int, float))}

    def save_settings(self) -> None:
        _write_text(os.path.join(self.root, SETTINGS_FILE),
                    json.dumps(self.settings, indent=2, sort_keys=True))

    def server_limit(self, key: str) -> int:
        lim = self.settings["limits"]
        per = (lim.get("servers") or {}).get(key)
        mb = per if per is not None else lim.get("server_default",
                                                  DEFAULT_SERVER_LIMIT_MB)
        return int(float(mb) * MB)

    def global_limit(self) -> int:
        return int(float(self.settings["limits"].get(
            "global", DEFAULT_GLOBAL_LIMIT_MB)) * MB)

    # ---- usage ------------------------------------------------------------
    def rescan(self) -> None:
        """Measure every top-level folder of the root from disk."""
        self._usage = {}
        for name in os.listdir(self.root):
            path = os.path.join(self.root, name)
            if os.path.isdir(path):
                self._usage[name] = _dir_size(path)

    def server_usage(self, key: str) -> int:
        if key not in self._usage:
            path = os.path.join(self.root, key)
            self._usage[key] = _dir_size(path) if os.path.isdir(path) else 0
        return self._usage[key]

    def global_usage(self) -> int:
        if not self._usage:
            self.rescan()
        return (sum(self._usage.values())
                + _file_size(os.path.join(self.root, SETTINGS_FILE)))

    def _add_usage(self, key: str, delta: int) -> None:
        self._usage[key] = max(0, self.server_usage(key) + delta)

    def check_room(self, key: str, path: str, new_size: int) -> None:
        """Raise unless writing `new_size` bytes at `path` (a file in server
        `key`'s folder, e.g. a `!store save`) fits the storage limits."""
        delta = new_size - _file_size(path)
        if delta <= 0:
            return
        msg = self._over_limit(key, delta)
        if msg:
            raise VTTError(msg.removeprefix("❌ "))

    def note_written(self, key: str, path: str, old_size: int) -> None:
        """Account for a file written in server `key`'s folder outside a
        commit (a `!store save`)."""
        self._add_usage(key, _file_size(path) - old_size)

    def _over_limit(self, key: str, delta: int) -> Optional[str]:
        """The refusal message when growing server `key` by `delta` bytes
        breaks a limit, else None."""
        if delta <= 0:
            return None
        used = self.server_usage(key)
        lim = self.server_limit(key)
        if used + delta > lim:
            return (f"❌ This server's storage limit is reached: "
                    f"{fmt_bytes(used)} used of {fmt_bytes(lim)}, and this "
                    f"would add {fmt_bytes(delta)}. Delete matches or saves "
                    f"to make room (the bot owner can raise the limit).")
        gused = self.global_usage()
        glim = self.global_limit()
        if gused + delta > glim:
            return (f"❌ The bot's total storage limit is reached "
                    f"({fmt_bytes(gused)} used of {fmt_bytes(glim)}). Tell "
                    f"the bot owner.")
        return None

    # ---- loading ----------------------------------------------------------
    def load_all(self, workspaces: Workspaces) -> List[str]:
        """Load every server folder into `workspaces`. Returns console notes
        (also printed): files that failed to load are moved to the server's
        corrupt/ folder and everything else still loads."""
        notes: List[str] = []
        if not self.persist:
            # Match persistence off: templates still load (they ignore it).
            for name in sorted(os.listdir(self.root)):
                path = os.path.join(self.root, name, "templates")
                if os.path.isdir(path) and valid_id(name):
                    notes.extend(self.load_templates(workspaces.get(name)))
            self.rescan()
            for n in notes:
                print(n)
            return notes
        self.purge_trash()
        for name in sorted(os.listdir(self.root)):
            path = os.path.join(self.root, name)
            if not os.path.isdir(path) or not valid_id(name):
                continue
            notes.extend(self.load_server(workspaces.get(name)))
        self.rescan()
        for n in notes:
            print(n)
        return notes

    def _quarantine(self, key: str, path: str, why: str) -> str:
        dest_dir = os.path.join(self.server_dir(key), "corrupt")
        os.makedirs(dest_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(dest_dir, f"{os.path.basename(path)}.{stamp}")
        try:
            shutil.move(path, dest)
        except OSError:
            pass
        rel = os.path.relpath(dest, self.root)
        return f"⚠️ [{key}] {os.path.relpath(path, self.root)} failed to load ({why}); moved to {rel}."

    def _read_json(self, path: str) -> Any:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def load_server(self, mgr: MatchManager) -> List[str]:
        """Load one server's folder into `mgr` (replacing what it holds)."""
        key = mgr.guild_key
        notes: List[str] = []
        sdir = self.server_dir(key)
        w = self._written.setdefault(key, self._blank_written())
        # Systems.
        systems = {"default": GameSystem("default", settings={})}
        sys_path = os.path.join(sdir, "systems.json")
        if os.path.exists(sys_path):
            try:
                raw = self._read_json(sys_path)
                loaded = {n: GameSystem.from_dict(d) for n, d in raw.items()}
                if not loaded:
                    raise ValueError("no systems")
                systems = loaded
            except Exception as ex:  # noqa: BLE001 - any bad file is quarantined
                notes.append(self._quarantine(key, sys_path, type(ex).__name__))
        mgr.systems = systems
        # Workspace.
        mgr.active_by_channel = {}
        mgr.default_system_name = "default" if "default" in systems \
            else sorted(systems)[0]
        mgr.default_system_per_channel = {}
        ws_path = os.path.join(sdir, "workspace.json")
        if os.path.exists(ws_path):
            try:
                raw = self._read_json(ws_path)
                mgr.active_by_channel = {str(k): str(v) for k, v in
                                         (raw.get("active_by_channel") or {}).items()}
                d = raw.get("default_system_name")
                if d in systems:
                    mgr.default_system_name = d
                mgr.default_system_per_channel = {
                    str(k): str(v) for k, v in
                    (raw.get("default_system_per_channel") or {}).items()
                    if v in systems}
            except Exception as ex:  # noqa: BLE001
                notes.append(self._quarantine(key, ws_path, type(ex).__name__))
        # Matches.
        mgr.matches = {}
        mdir = os.path.join(sdir, "matches")
        if os.path.isdir(mdir):
            for fname in sorted(os.listdir(mdir)):
                if not fname.endswith(".json"):
                    continue
                mid = fname[:-5]
                fpath = os.path.join(mdir, fname)
                try:
                    m = self._load_match(mgr, mid, fpath)
                except Exception as ex:  # noqa: BLE001
                    notes.append(self._quarantine(key, fpath, type(ex).__name__))
                    continue
                hnote = self._load_history(mgr, m)
                if hnote:
                    notes.append(hnote)
                mgr.matches[mid] = m
        mgr.active_by_channel = {ch: mid for ch, mid in mgr.active_by_channel.items()
                                 if mid in mgr.matches}
        notes.extend(self.load_templates(mgr))
        self._remember(mgr, w)
        return notes

    def _load_match(self, mgr: MatchManager, mid: str, path: str) -> Match:
        if not valid_id(mid):
            raise ValueError("bad match id")
        m = Match.from_dict(self._read_json(path))
        if m.id != mid:
            raise ValueError("id doesn't match the file name")
        if m.system_name not in mgr.systems:
            m.system_name = mgr.default_system_name
        m.rules = mgr._build_rules_dict(mgr.systems[m.system_name])
        return m

    def _load_history(self, mgr: MatchManager, m: Match) -> Optional[str]:
        key = mgr.guild_key
        hdir = self._history_dir(key, m.id)
        ipath = os.path.join(hdir, "index.json")
        if not os.path.exists(ipath):
            return None
        try:
            raw = self._read_json(ipath)
            m.history = MatchHistory.from_index(
                raw, lambda seq, k=key, mid=m.id: self._snap_file(k, mid, seq))
        except Exception as ex:  # noqa: BLE001
            m.history = MatchHistory()
            dest_dir = os.path.join(self.server_dir(key), "corrupt")
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, f"{m.id}-history.{time.strftime('%Y%m%d-%H%M%S')}")
            try:
                shutil.move(hdir, dest)
            except OSError:
                pass
            return (f"⚠️ [{key}] the undo history of `{m.id}` failed to load "
                    f"({type(ex).__name__}); moved to "
                    f"{os.path.relpath(dest, self.root)}.")
        return None

    # ---- what's on disk ---------------------------------------------------
    @staticmethod
    def _blank_written() -> Dict[str, Any]:
        return {"workspace": None, "systems": None, "matches": {},
                "index": {}, "seqs": {}, "hist": {}}

    @staticmethod
    def _workspace_dict(mgr: MatchManager) -> Dict[str, Any]:
        return {"active_by_channel": dict(mgr.active_by_channel),
                "default_system_name": mgr.default_system_name,
                "default_system_per_channel": dict(mgr.default_system_per_channel)}

    @staticmethod
    def _systems_dict(mgr: MatchManager) -> Dict[str, Any]:
        return {n: s.to_dict() for n, s in mgr.systems.items()}

    def _persists_history(self, m: Match) -> bool:
        return bool(m.rules.get("persist_undo_history", True))

    def _remember(self, mgr: MatchManager, w: Dict[str, Any]) -> None:
        """Record the in-memory state as what's on disk (after a load)."""
        w["workspace"] = _dump(self._workspace_dict(mgr))
        w["systems"] = _dump(self._systems_dict(mgr))
        w["matches"] = {mid: _dump(_disk_state(m.to_dict(include_history=False)))
                        for mid, m in mgr.matches.items()}
        w["index"] = {}
        w["seqs"] = {}
        w["hist"] = {}
        self._trim_before[mgr.guild_key] = {
            mid: bool(m.rules.get("storage_trim_autosaves", True))
            for mid, m in mgr.matches.items()}
        for mid, m in mgr.matches.items():
            h = m.history
            w["hist"][mid] = (h._seq, h._turn_index)
            if os.path.exists(os.path.join(self._history_dir(mgr.guild_key, mid),
                                           "index.json")):
                w["index"][mid] = _dump(h.index())
                w["seqs"][mid] = {s.sequence for s in h.all_snapshots()}

    # ---- committing -------------------------------------------------------
    def _plan(self, mgr: MatchManager, w: Dict[str, Any]
              ) -> Tuple[Dict[str, Optional[str]], Dict[str, Any]]:
        """The writes (path -> text) and deletes (path -> None) that bring the
        disk in line with `mgr`, plus what `_written` becomes after them."""
        key = mgr.guild_key
        sdir = self.server_dir(key)
        plan: Dict[str, Optional[str]] = {}
        after = {"workspace": w["workspace"], "systems": w["systems"],
                 "matches": dict(w["matches"]), "index": dict(w["index"]),
                 "seqs": {k: set(v) for k, v in w["seqs"].items()},
                 "hist": dict(w["hist"])}
        text = _dump(self._workspace_dict(mgr))
        if text != w["workspace"]:
            plan[os.path.join(sdir, "workspace.json")] = text
            after["workspace"] = text
        text = _dump(self._systems_dict(mgr))
        if text != w["systems"]:
            plan[os.path.join(sdir, "systems.json")] = text
            after["systems"] = text
        for mid, m in mgr.matches.items():
            text = _dump(_disk_state(m.to_dict(include_history=False)))
            if text != w["matches"].get(mid):
                plan[self._match_file(key, mid)] = text
                after["matches"][mid] = text
            h = m.history
            after["hist"][mid] = (h._seq, h._turn_index)
            old_seqs = w["seqs"].get(mid, set())
            if self._persists_history(m):
                snaps = h.all_snapshots()
                seqs = {s.sequence for s in snaps}
                for s in snaps:
                    if s.sequence not in old_seqs:
                        plan[self._snap_file(key, mid, s.sequence)] = \
                            self._snap_text(s)
                for seq in old_seqs - seqs:
                    plan[self._snap_file(key, mid, seq)] = None
                itext = _dump(h.index())
                if itext != w["index"].get(mid):
                    plan[os.path.join(self._history_dir(key, mid),
                                      "index.json")] = itext
                    after["index"][mid] = itext
                after["seqs"][mid] = seqs
            elif mid in w["index"]:
                # persist_undo_history was switched off: drop the files.
                plan[os.path.join(sdir, "matches", mid)] = None
                after["index"].pop(mid, None)
                after["seqs"].pop(mid, None)
        for mid in list(w["matches"]):
            if mid not in mgr.matches:
                plan[self._match_file(key, mid)] = None
                plan[os.path.join(sdir, "matches", mid)] = None
                for d in ("matches", "index", "seqs", "hist"):
                    after[d].pop(mid, None)
        return plan, after

    @staticmethod
    def _snap_text(s) -> str:
        d = s.to_dict()
        d["state"] = _disk_state(d["state"])
        return _dump(d)

    @staticmethod
    def _plan_delta(plan: Dict[str, Optional[str]]) -> int:
        delta = 0
        for path, text in plan.items():
            old = _dir_size(path) if os.path.isdir(path) else _file_size(path)
            delta += (len(text.encode("utf-8")) if text is not None else 0) - old
        return delta

    def _trim_candidates(self, mgr: MatchManager) -> List[Tuple[str, str, int, int]]:
        """Autosaves that may be cut to fit a limit, oldest first:
        (timestamp, match id, sequence, bytes it frees)."""
        out = []
        before = self._trim_before.get(mgr.guild_key, {})
        for mid, m in mgr.matches.items():
            # Eligible when the rule is on now OR was on before this command,
            # so the command turning it off can still make room for itself.
            trim = (m.rules.get("storage_trim_autosaves", True)
                    or before.get(mid, False))
            if not (self._persists_history(m) and trim):
                continue
            for s in (m.history.round_saves + m.history.turn_saves
                      + m.history.command_saves):
                path = self._snap_file(mgr.guild_key, mid, s.sequence)
                size = _file_size(path)
                if not size:
                    size = len(self._snap_text(s).encode("utf-8"))
                out.append((s.timestamp, mid, s.sequence, size))
        out.sort(key=lambda t: (t[0], t[2]))
        return out

    def commit(self, mgr: MatchManager) -> List[str]:
        """Write what changed in server `mgr` since the last commit. Returns
        lines to show the user (a trim note, or the rollback warning)."""
        if not self.persist:
            return []
        key = mgr.guild_key
        w = self._written.setdefault(key, self._blank_written())
        msgs: List[str] = []
        plan, after = self._plan(mgr, w)
        if not plan:
            self._maybe_purge_trash()
            return msgs
        delta = self._plan_delta(plan)
        refusal = self._over_limit(key, delta)
        if refusal:
            # Cut the oldest autosaves first (storage_trim_autosaves), if that
            # alone makes room; otherwise undo the command.
            cut: Dict[str, List[int]] = {}
            freed = 0
            for _ts, mid, seq, size in self._trim_candidates(mgr):
                if self._over_limit(key, delta - freed) is None:
                    break
                cut.setdefault(mid, []).append(seq)
                freed += size
            if cut and self._over_limit(key, delta - freed) is None:
                for mid, seqs in cut.items():
                    mgr.matches[mid].history.drop_sequences(seqs)
                n = sum(len(v) for v in cut.values())
                msgs.append(f"🗄️ Storage limit: cut the {n} oldest undo "
                            f"autosave(s) to make room.")
                plan, after = self._plan(mgr, w)
                delta = self._plan_delta(plan)
                refusal = self._over_limit(key, delta)
            if refusal:
                self.rollback(mgr)
                return [refusal + " The command was undone."]
        for path, text in plan.items():
            if text is None:
                _remove_path(path)
        for path, text in plan.items():
            if text is not None:
                _write_text(path, text)
        self._written[key] = after
        self._add_usage(key, delta)
        self._trim_before[key] = {
            mid: bool(m.rules.get("storage_trim_autosaves", True))
            for mid, m in mgr.matches.items()}
        # Persisted snapshots read their state back from disk when needed.
        for mid, m in mgr.matches.items():
            if self._persists_history(m):
                for s in m.history.all_snapshots():
                    s.path = self._snap_file(key, mid, s.sequence)
                    s.release_state()
        self._maybe_purge_trash()
        return msgs

    def rollback(self, mgr: MatchManager) -> None:
        """Put server `mgr` back to what is on disk (the state after the last
        commit): matches in place (live objects and their runtime state kept),
        their undo history, the channel pointers and the systems."""
        from action import _rollback_match
        key = mgr.guild_key
        w = self._written.setdefault(key, self._blank_written())
        if w["systems"] is not None:
            mgr.systems = {n: GameSystem.from_dict(d)
                           for n, d in json.loads(w["systems"]).items()}
        if w["workspace"] is not None:
            ws = json.loads(w["workspace"])
            mgr.active_by_channel = dict(ws.get("active_by_channel") or {})
            mgr.default_system_name = ws.get("default_system_name", "default")
            mgr.default_system_per_channel = dict(
                ws.get("default_system_per_channel") or {})
        for mid in [mid for mid in mgr.matches if mid not in w["matches"]]:
            del mgr.matches[mid]
        for mid, text in w["matches"].items():
            state = json.loads(text)
            m = mgr.matches.get(mid)
            if m is None:
                m = Match.from_dict(state)
                mgr.matches[mid] = m
            elif _dump(_disk_state(m.to_dict(include_history=False))) != text:
                _rollback_match(m, mgr, state)
            if m.system_name in mgr.systems:
                m.rules = mgr._build_rules_dict(mgr.systems[m.system_name])
            itext = w["index"].get(mid)
            if itext is not None:
                m.history = MatchHistory.from_index(
                    json.loads(itext),
                    lambda seq, k=key, i=mid: self._snap_file(k, i, seq))
            elif mid in w["hist"]:
                seq, turn = w["hist"][mid]
                h = m.history
                h.drop_sequences([s.sequence for s in h.all_snapshots()
                                  if s.sequence > seq])
                for name in [n for n, s in h.manual_saves.items()
                             if s.sequence > seq]:
                    del h.manual_saves[name]
                h._seq, h._turn_index = seq, turn

    def commit_all(self, workspaces: Workspaces) -> None:
        """Write every workspace (shutdown). Limits are not enforced: the
        state was already accepted command by command."""
        if not self.persist:
            return
        for _key, mgr in workspaces.items():
            w = self._written.setdefault(mgr.guild_key, self._blank_written())
            plan, after = self._plan(mgr, w)
            for path, text in plan.items():
                if text is None:
                    _remove_path(path)
                else:
                    _write_text(path, text)
            self._written[mgr.guild_key] = after

    def forget(self, key: str) -> None:
        """Treat server `key` as having nothing on disk (after its files were
        moved away), so the next commit writes everything fresh."""
        self._written[key] = self._blank_written()

    # ---- trash (`!server wipe`) ------------------------------------------
    def _maybe_purge_trash(self) -> None:
        if time.time() - self._last_trash_purge > 3600:
            self.purge_trash()

    def purge_trash(self) -> None:
        """Delete trash entries older than 24 hours."""
        self._last_trash_purge = time.time()
        root = self._trash_root()
        if not os.path.isdir(root):
            return
        now = time.time()
        for server in os.listdir(root):
            sdir = os.path.join(root, server)
            if not os.path.isdir(sdir):
                continue
            for entry in os.listdir(sdir):
                try:
                    stamp = float(entry.split("-", 1)[0])
                except ValueError:
                    continue
                if now - stamp > TRASH_SECONDS:
                    _remove_path(os.path.join(sdir, entry))
        self._usage[TRASH_DIR] = _dir_size(root)

    def trash_entries(self, key: str) -> List[Tuple[float, str, str]]:
        """(time, scope, path) of server `key`'s trash entries, newest first."""
        sdir = os.path.join(self._trash_root(), key)
        out = []
        if os.path.isdir(sdir):
            for entry in os.listdir(sdir):
                stamp, _, scope = entry.partition("-")
                try:
                    t = float(stamp)
                except ValueError:
                    continue
                if time.time() - t <= TRASH_SECONDS:
                    out.append((t, scope, os.path.join(sdir, entry)))
        out.sort(reverse=True)
        return out

    def move_to_trash(self, mgr: MatchManager, scope: str) -> str:
        """Move the files a `!server wipe <scope>` removes into the trash and
        return the trash entry's path. `matches`: the matches folder (with
        their histories). `all`: also systems.json, saves/ and templates/.
        A copy of
        workspace.json goes along so an undo can restore the channel
        pointers."""
        if not self.persist:
            raise VTTError(NO_PERSIST_WIPE)
        key = mgr.guild_key
        sdir = self.server_dir(key)
        # Make sure what's in memory is what gets trashed.
        self.commit_all_one(mgr)
        entry = os.path.join(self._trash_root(), key, f"{time.time():.3f}-{scope}")
        os.makedirs(entry, exist_ok=True)
        moved = ["matches"] + (["systems.json", "saves", "templates"]
                               if scope == "all" else [])
        for name in moved:
            src = os.path.join(sdir, name)
            if os.path.exists(src):
                shutil.move(src, os.path.join(entry, name))
        ws = os.path.join(sdir, "workspace.json")
        if os.path.exists(ws):
            shutil.copy2(ws, os.path.join(entry, "workspace.json"))
            if scope == "all":
                os.remove(ws)
        self._usage[key] = _dir_size(sdir) if os.path.isdir(sdir) else 0
        self._usage[TRASH_DIR] = _dir_size(self._trash_root())
        self.forget(key)
        return entry

    def commit_all_one(self, mgr: MatchManager) -> None:
        if not self.persist:
            return
        w = self._written.setdefault(mgr.guild_key, self._blank_written())
        plan, after = self._plan(mgr, w)
        for path, text in plan.items():
            if text is None:
                _remove_path(path)
            else:
                _write_text(path, text)
        self._written[mgr.guild_key] = after

    def restore_from_trash(self, mgr: MatchManager, entry: str, scope: str) -> None:
        """Move a trash entry back into the server's folder and reload the
        server from disk."""
        if not self.persist:
            raise VTTError(NO_PERSIST_WIPE)
        key = mgr.guild_key
        sdir = self.server_dir(key)
        os.makedirs(sdir, exist_ok=True)
        for name in ("matches", "systems.json", "saves", "templates"):
            src = os.path.join(entry, name)
            if not os.path.exists(src):
                continue
            dest = os.path.join(sdir, name)
            _remove_path(dest)
            shutil.move(src, dest)
        ws_src = os.path.join(entry, "workspace.json")
        ws_dest = os.path.join(sdir, "workspace.json")
        if os.path.exists(ws_src):
            if scope == "all" or not os.path.exists(ws_dest):
                shutil.copy2(ws_src, ws_dest)
            else:
                # A matches-only wipe kept the defaults; restore the channel
                # pointers alone.
                try:
                    old = self._read_json(ws_src)
                    cur = self._read_json(ws_dest)
                    cur["active_by_channel"] = old.get("active_by_channel") or {}
                    _write_text(ws_dest, json.dumps(cur, sort_keys=True))
                except (OSError, ValueError):
                    pass
        _remove_path(entry)
        self.forget(key)
        self.load_server(mgr)
        self._usage[key] = _dir_size(sdir)
        self._usage[TRASH_DIR] = _dir_size(self._trash_root())


DATA_DIR_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

NO_PERSIST_WIPE = ("Match persistence is off (local_settings.json), so this session "
                   "keeps its matches in memory only: restart for a clean "
                   "state, or `!match delete <id>`. A wipe would act on the "
                   "data left on disk, which this session ignores.")

# cli.py / gui.py settings (the Discord bot always persists). A JSON file
# beside the code, created with these defaults on first start.
LOCAL_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "local_settings.json")
_LOCAL_SETTINGS_COMMENT = [
    "Settings for cli.py and gui.py (the Discord bot always saves its data).",
    "match_persistence: true = matches, undo history, game systems and "
    "channel pointers are written to data/local after every command and "
    "loaded at the next start.",
    "match_persistence: false = every session starts empty and writes none "
    "of that. Whatever is already in data/local is IGNORED and LEFT ON DISK "
    "untouched; setting match_persistence back to true loads it again.",
    "Either way, entity templates (!template) are saved to and loaded from "
    "data/local/templates, manual saves (!store save, !history export) go "
    "to data/local/saves, and data/local/sprites is used.",
]


def load_local_settings(path: str = LOCAL_SETTINGS_FILE) -> Dict[str, Any]:
    """The local surfaces' settings: {"match_persistence": bool}. A missing
    file is created with the defaults (match persistence off) and its
    _comment notes; an unreadable one is reported and the defaults are
    used."""
    settings: Dict[str, Any] = {"match_persistence": False}
    if not os.path.exists(path):
        try:
            _write_text(path, json.dumps(
                {"_comment": _LOCAL_SETTINGS_COMMENT,
                 "match_persistence": False},
                indent=2) + "\n")
        except OSError as ex:
            print(f"⚠️ Couldn't create {os.path.basename(path)} ({ex}).")
        return settings
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as ex:
        print(f"⚠️ {os.path.basename(path)} couldn't be read ({ex}); "
              f"match persistence stays off.")
        return settings
    key = "match_persistence"
    if key not in data and "persistence" in data:
        print(f"⚠️ {os.path.basename(path)}: `persistence` is now called "
              f"`match_persistence` (templates are always saved); rename it "
              f"in the file. Using its value for now.")
        key = "persistence"
    val = data.get(key, False)
    if not isinstance(val, bool):
        print(f"⚠️ {os.path.basename(path)}: `match_persistence` must be "
              f"true or false; it stays off.")
        val = False
    settings["match_persistence"] = val
    return settings


def local_persistence_note(settings: Dict[str, Any]) -> Optional[str]:
    """The startup warning while local match persistence is off, else None."""
    if settings.get("match_persistence"):
        return None
    return ("⚠️ Match persistence is off: this session's matches and undo "
            "history are lost when you close it, and nothing already saved "
            "in data/local is loaded (it stays on disk). Entity templates "
            "(`!template`) are still saved and loaded, and manual saves "
            "(`!store save`) still go to data/local/saves. To keep sessions, "
            "set \"match_persistence\": true in local_settings.json.")


def open_workspaces(root: str = DATA_DIR_DEFAULT,
                    persist: bool = True) -> Workspaces:
    """Workspaces backed by the data folder at `root`, with every server
    already loaded from it (the bot / CLI / GUI startup). persist=False:
    start empty and write nothing (Storage.persist)."""
    ws = Workspaces()
    ws.storage = Storage(root, persist=persist)
    ws.storage.load_all(ws)
    return ws


def reload_workspaces(workspaces: Workspaces) -> List[str]:
    """Write everything, then rebuild `workspaces` from its data folder in
    place (the `!as restart` test hook: what a bot restart would load)."""
    st = workspaces.storage
    st.commit_all(workspaces)
    fresh = Workspaces()
    fresh.storage = Storage(st.root, persist=st.persist)
    notes = fresh.storage.load_all(fresh)
    workspaces._by_key = fresh._by_key
    workspaces.storage = fresh.storage
    for _key, mgr in workspaces.items():
        mgr.workspaces = workspaces
    return notes


def storage_of(mgr: Any) -> Optional[Storage]:
    """The Storage behind a manager, or None when nothing persists."""
    ws = getattr(mgr, "workspaces", None)
    return getattr(ws, "storage", None) if ws is not None else None
