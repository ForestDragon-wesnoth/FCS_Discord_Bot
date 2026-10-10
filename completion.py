# completion.py — Tab completion for typed commands, shared by the GUI and
# the CLI (readline). Surface-agnostic: complete() takes the text before the
# cursor and returns where the current word starts plus the candidates.
#
# What completes:
#   - the first word: command roots and the active match's aliases;
#   - later words: whatever the command's usage strings (the same ones the
#     stray-word check reads) allow at that position. Literal words
#     (subcommands, `on|off`, `force`) complete as themselves; a `<slot>`
#     completes from the names that fit it (unit ids of the channel's match,
#     filtered by its POV; template, match, system, rule, macro, table,
#     status, zone, ... names; a unit's or a template's var paths one
#     segment at a time), and a slot whose kind isn't clear from its name
#     completes from a general pool of names. `key=` options of the matched
#     forms complete too.
#   - after ` ; ` in `!batch` / `!foreach`, the next command (no `!`).
# Nothing here changes state: completion only reads.
from __future__ import annotations

import re
import shlex
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import logic
from vtt_commands import (registry, _usage_tokens, _split_top, _usage_tails,
                          _is_option_group, _is_variadic, _OPTION_WORD,
                          _SUB_ALIASES, _query_pov, _pov_hides, _MAP_LAYERS)

# Placeholder names whose words are free values (numbers, formulas, text,
# new names): nothing to offer.
_FREE_SLOTS = {
    "x", "y", "x1", "y1", "x2", "y2", "n", "value", "formula", "body", "dice",
    "hp", "maxhp", "w", "h", "delay", "delta", "level", "duration", "count",
    "r", "radius", "dx", "dy", "expansion", "params", "spec", "json", "text",
    "phrase", "condition", "effect", "new_name", "new_id", "init", "field",
    "base", "tag", "op", "0-100", "MB", "csv", "row", "legend", "char", "color",
    "r,g,b", "predicate", "selector", "selector_a", "selector_b", "reason",
    "args", "tokens", "label", "user", "server", "metric",
    "anchor", "word", "type", "template_text", "winner", "stat", "pid",
    "passive_id", "path", "var.path", "key", "dest_path", "stash_path",
    "shape args", "cmd1", "cmd2",
}
# Placeholders that name a unit.
_ENTITY_SLOTS = {
    "eid", "id", "unit", "entity_id", "part_id", "rider", "vehicle", "parent",
    "a", "b", "id1", "id2", "src_id", "dest_id", "head", "seg_id", "target",
    "from", "to", "part",
}
# Slots that name something NEW: the existing names would only be in the way.
_NEW_NAME = {("ent", "add", "id"), ("match", "new", "id"),
             ("part", "add", "part_id"), ("part", "segment", "seg_id"),
             ("system", "new", "name"), ("zone", "new", "name"),
             ("status", "def", "name"), ("ent", "add", "name"),
             ("part", "add", "name"), ("part", "segment", "name")}
_DIRECTIONS = sorted(logic.ALLOWED_DIRECTIONS)
_REGIONS = sorted(logic.Entity._PART_REGIONS) if hasattr(
    logic.Entity, "_PART_REGIONS") else []
# A runaway guard for the grammar walk (usage strings are small; this only
# bounds a pathological one).
_WALK_BUDGET = 20000
# The most candidates a listing shows (the caller says how many were left).
LIST_CAP = 120


# ---- usage grammar ---------------------------------------------------------
# A usage tail parses into nodes: ("lit", word) / ("slot", name) consume one
# word; ("seq", [nodes]); ("alt", [seq, ...]); ("opt", node) may be skipped;
# ("star", node) repeats; ("keys", [option keys]) consumes nothing.

def _bare(word: str, in_group: bool) -> tuple:
    """A bare usage word: a literal, except that inside `<..>` / `[..]` a
    known placeholder name (`<eid | x y>`, `[name]`) is a slot."""
    if word.endswith("..."):
        return ("star", _bare(word[:-3], True))
    if in_group and (word in _FREE_SLOTS or word in _ENTITY_SLOTS
                     or word in _KIND_SLOTS
                     or not re.fullmatch(r"[A-Za-z_][\w:-]*", word)):
        return ("slot", word)          # `=n`, `+n`: a value, not a word
    if ":" in word:
        # `group:NAME`: the literal part, continued by the name.
        return ("lit", word.split(":", 1)[0].lower() + ":")
    return ("lit", word.lower())


def _parse_seq(text: str, in_group: bool) -> tuple:
    nodes: List[tuple] = []
    for tok in _usage_tokens(text):
        tok = tok.strip('"')
        if not tok:
            continue
        if tok in ("...", "…"):
            if nodes:
                nodes[-1] = ("star", nodes[-1])
            continue
        if _is_option_group(tok) and re.match(r"\[?[A-Za-z_]", tok):
            body = tok[1:-1] if tok.startswith("[") else tok
            keys = [w.split("=", 1)[0] for w in _usage_tokens(body) if "=" in w]
            nodes.append(("keys", keys))
            continue
        if tok[0] in "<[" and tok[-1] in ">]":
            inner = tok[1:-1].strip()
            variadic = _is_variadic(inner)
            if variadic:
                inner = inner.rstrip(".…").strip()
            if tok[0] == "<" and "|" not in inner and not any(
                    c in inner for c in "<[") or (
                    tok[0] == "<" and "|" not in inner
                    and inner[:1] not in "<["):
                # `<eid>`, `<request id>`, `<dir[,dir...]>`: one value.
                node: tuple = ("slot", inner.split("[", 1)[0].strip())
            else:
                alts = [_parse_seq(a, True) for a in _split_top(inner) if a]
                node = alts[0] if len(alts) == 1 else ("alt", alts)
            if variadic:
                node = ("star", node)
            if tok[0] == "[":
                node = ("opt", node)
            nodes.append(node)
            continue
        nodes.append(_bare(tok, in_group))
    return ("seq", nodes)


class _Walk:
    """Walks typed words through a grammar; collects what may come next
    (expectations, with the words each slot consumed on the way) and the
    option keys seen."""

    def __init__(self, words: List[str]):
        self.words = words
        self.expect: List[Tuple[tuple, Dict[str, str]]] = []
        self.keys: Set[str] = set()
        self.budget = _WALK_BUDGET

    def visit(self, node: tuple, i: int, binds: Dict[str, str],
              k: Callable[[int, Dict[str, str]], None]) -> None:
        self.budget -= 1
        if self.budget <= 0:
            return
        kind = node[0]
        n = len(self.words)
        if kind in ("lit", "slot"):
            if i == n:
                self.expect.append((node, binds))
                return
            if kind == "lit" and self.words[i].lower() != node[1]:
                return
            nb = binds
            if kind == "slot":
                nb = dict(binds)
                nb.setdefault(node[1], self.words[i])
            k(i + 1, nb)
        elif kind == "seq":
            items = node[1]

            def go(j: int, idx: int, b: Dict[str, str]) -> None:
                if idx == len(items):
                    k(j, b)
                else:
                    self.visit(items[idx], j, b,
                               lambda e, b2: go(e, idx + 1, b2))
            go(i, 0, binds)
        elif kind == "alt":
            for s in node[1]:
                self.visit(s, i, binds, k)
        elif kind == "opt":
            k(i, binds)
            self.visit(node[1], i, binds, k)
        elif kind == "star":
            k(i, binds)
            self.visit(node[1], i, binds,
                       lambda e, b2: e > i and self.visit(node, e, b2, k))
        elif kind == "keys":
            if i == n:
                self.keys.update(node[1])
            k(i, binds)


def _grammar(root: str) -> Optional[tuple]:
    """Every form of `root` (its own usage and each subcommand's) as one
    alternation. A root usage that is only `<subcommand> ...` adds nothing
    the subcommand forms don't."""
    meta = registry._help.get(root) or {}
    tails = _usage_tails(meta.get("usage") or "", root)
    # `<subcommand> ...` / `<save|spawn|...> ...`: a listing of the
    # subcommands, whose own forms say more.
    tails = [t for t in tails
             if not (len(_usage_tokens(t)) == 2
                     and _usage_tokens(t)[0].startswith("<")
                     and _usage_tokens(t)[1] in ("...", "…"))]
    for sub in (meta.get("subs") or {}).values():
        tails += _usage_tails(sub.get("usage") or "", root)
    alts = [_parse_seq(t, False) for t in tails if t]
    return ("alt", alts) if alts else None


# ---- name sources ----------------------------------------------------------

def _match(mgr, ctx):
    mid = mgr.get_active_for_channel(getattr(ctx, "channel_key", None))
    return mgr.matches.get(mid) if mid else None


def _entity_ids(mgr, ctx) -> List[str]:
    m = _match(mgr, ctx)
    if m is None:
        return []
    try:
        pov = _query_pov(ctx, m)
    except Exception:
        pov = None
    return sorted(e for e in m.entities if not _pov_hides(m, pov, e))


def _corpse_ids(mgr, ctx) -> List[str]:
    m = _match(mgr, ctx)
    if m is None:
        return []
    try:
        if _query_pov(ctx, m) is not None:
            return []          # corpses follow their own visibility rule
    except Exception:
        return []
    return sorted({c[2] for c in m.all_corpses()})


def _teams(mgr, ctx) -> List[str]:
    m = _match(mgr, ctx)
    if m is None:
        return []
    tv = str(m.rules.get("team_var", "team"))
    out = {str(e.vars.get(tv)) for e in m.entities.values()
           if e.vars.get(tv) is not None}
    out |= {str(t) for t in m.team_data} | {str(t) for t in m.team_colors}
    return sorted(out)


def _var_paths(tree: Any, prefix: str) -> List[str]:
    """Var-path completions one segment at a time: the children of the
    container `prefix` names (up to its last dot) that start with what
    follows it; a container ends with `.` so the next Tab goes inside.
    Engine keys (`__x`) show only once the segment typed starts with `_`."""
    head, _, part = prefix.rpartition(".")
    node = tree
    for seg in head.split(".") if head else []:
        node = node.get(seg) if isinstance(node, dict) else None
    if not isinstance(node, dict):
        return []
    out = []
    for k, v in node.items():
        k = str(k)
        if k.startswith("__") and not part.startswith("_"):
            continue
        full = f"{head}.{k}" if head else k
        out.append(full + "." if isinstance(v, dict) and v else full)
    return sorted(out)


def _template_tree(mgr, name: Optional[str]) -> Any:
    t = mgr.templates.get(name or "")
    if not isinstance(t, dict):
        return None
    return t.get("data") if t.get("kind") == "partial" else t


def _general_pool(mgr, ctx) -> List[str]:
    m = _match(mgr, ctx)
    out = set(_entity_ids(mgr, ctx)) | set(mgr.templates) | set(mgr.matches) \
        | set(mgr.systems)
    if m is not None:
        out |= set(m.zones) | set(m.status_definitions) | set(m.macros) \
            | set(m.tables) | set(m.groups)
    return sorted(str(x) for x in out)


def _slot_names(slot: str, root: str, sub: Optional[str],
                binds: Dict[str, str], word: str, mgr, ctx) -> List[str]:
    """The candidates for one placeholder, from its name and command."""
    m = _match(mgr, ctx)
    if (root, sub, slot) in _NEW_NAME or not re.fullmatch(
            r"[A-Za-z_][\w.,: -]*", slot):
        return []                      # a new name, or `=n` / `+n`
    if (root in ("approve", "deny") and slot == "id") or slot == "request id":
        return sorted(m.pending_requests) if m is not None else []
    if root == "help" and slot == "sub":
        meta = registry._help.get((binds.get("command") or "").lower()) or {}
        return sorted(meta.get("subs") or {})
    ent = binds.get("eid") or binds.get("id") or binds.get("unit") \
        or binds.get("entity_id")
    if slot in ("path", "var.path", "key", "stash_path", "dest_path",
                "template_ref"):
        if root == "template" and slot == "path" and "name" in binds:
            return _var_paths(_template_tree(mgr, binds.get("name")), word)
        if root == "team" and m is not None:
            return _var_paths(m.team_data.get(binds.get("team", "")), word)
        if root == "match" and m is not None:
            return _var_paths(m.vars, word)
        if root == "zone" and m is not None:
            return _var_paths((m.zones.get(binds.get("name", "")) or {})
                              .get("data"), word)
        if root == "system" and slot == "key":
            return sorted(logic.RULES_REGISTRY)
        if slot == "dest_path":
            ent = binds.get("dest_id")
        if m is not None and ent in _entity_ids(mgr, ctx):
            paths = _var_paths(m.entities[ent].vars, word)
            if slot == "template_ref":
                paths += _entity_ids(mgr, ctx)
            return paths
        return _entity_ids(mgr, ctx) if slot == "template_ref" else []
    if slot in _ENTITY_SLOTS and not (root == "match" and slot == "id"):
        if root == "template" and slot == "part":
            t = mgr.templates.get(binds.get("name", ""))
            parts = t.get("parts") if isinstance(t, dict) else None
            if isinstance(parts, dict):
                return sorted(str(p) for p in parts)
            return sorted(str(p.get("id", "")).replace(logic.TEMPLATE_ROOT + "_", "")
                          for p in parts or [] if isinstance(p, dict))
        return _entity_ids(mgr, ctx)
    if slot == "corpse_id":
        return _corpse_ids(mgr, ctx)
    if slot in ("template", "template_ref"):
        return sorted(mgr.templates)
    if slot == "partial":
        return sorted(n for n, t in mgr.templates.items()
                      if isinstance(t, dict) and t.get("kind") == "partial")
    if slot in ("system", "dest_match") or (root == "match" and slot == "id"):
        return sorted(mgr.systems if slot == "system" else mgr.matches)
    if slot == "rule":
        return sorted(logic.RULES_REGISTRY)
    if slot == "team":
        return _teams(mgr, ctx)
    if slot in ("when", "hook_name"):
        return sorted(logic.HOOK_NAMES) + ["event:"]
    if slot == "dir":
        return _DIRECTIONS
    if slot == "region":
        return _REGIONS
    if slot == "layers":
        return list(_MAP_LAYERS)
    if slot in ("cmd", "command", "subcommand"):
        return sorted(registry._handlers)
    if slot == "token":
        return sorted(m.status_definitions) if m is not None else []
    if slot == "slot" and m is not None:
        holder = binds.get("vehicle")
        if holder is None and binds.get("rider") in m.entities:
            holder = m.entities[binds["rider"]].mounted_on
        e = m.entities.get(holder or "")
        slots = e.vars.get("slots") if e is not None else None
        return sorted(str(s) for s in slots) if isinstance(slots, dict) else []
    if slot == "name":
        if root == "template":
            return sorted(mgr.templates)
        if root == "system":
            return sorted(mgr.systems)
        if m is None:
            return []
        if root in ("action", "ent") and (root == "action" or sub == "action"):
            if ent in m.entities:
                from action import discover_actions
                return sorted(discover_actions(m.entities[ent], m.rules))
            return []
        if root == "ent" and sub == "status":
            own = set(m.entities[ent].status) if ent in m.entities else set()
            return sorted(own | set(m.status_definitions))
        by_root = {
            "macro": m.macros, "table": m.tables, "alias": m.aliases,
            "func": m.formula_functions, "status": m.status_definitions,
            "zone": m.zones, "watch": m.watchers, "tile": m.tile_templates,
            "history": m.history.manual_saves,
        }
        if root == "ent" and sub == "group":
            return sorted(m.groups)
        if root == "match" and sub == "templates":
            return sorted(m.template_pins)
        if root in by_root:
            return sorted(str(x) for x in by_root[root])
    if slot in _FREE_SLOTS:
        return []
    return _general_pool(mgr, ctx)


# Placeholders _slot_names knows a source for (used to tell slots from
# literal words inside groups).
_KIND_SLOTS = {"name", "template", "template_ref", "partial", "system",
               "dest_match", "rule", "team", "when", "hook_name", "dir",
               "region", "layers", "cmd", "command", "subcommand", "token",
               "slot", "corpse_id", "key", "path", "sub"}


# ---- the entry point -------------------------------------------------------

def _split_words(text: str) -> Optional[List[str]]:
    """The words of `text` (quotes honoured), or None inside an open quote."""
    try:
        lex = shlex.shlex(text, posix=True)
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        return None


def _command_candidates(words: List[str], word: str, mgr, ctx) -> List[str]:
    """Candidates for `word` after the command words `words` (root first,
    no `!`)."""
    if not words:
        m = _match(mgr, ctx)
        names = set(registry._handlers) | (set(m.aliases) if m else set())
        return sorted(names)
    root = words[0].lower()
    m = _match(mgr, ctx)
    if root not in registry._handlers and m is not None and root in m.aliases:
        exp = _split_words(m.aliases[root]) or []
        if exp:
            return _command_candidates(exp + words[1:], word, mgr, ctx)
    if root in ("batch", "foreach") and ";" in words:
        tail = words[len(words) - words[::-1].index(";"):]
        return _command_candidates([w.lstrip("!") for w in tail], word,
                                   mgr, ctx)
    gram = _grammar(root)
    if gram is None:
        return []
    args = [w for w in words[1:] if not _OPTION_WORD.match(w)]
    if args:
        canon = _SUB_ALIASES.get((root, args[0].lower()))
        if canon:
            args[0] = canon
    sub = args[0].lower() if args else None
    walk = _Walk(args)
    walk.visit(gram, 0, {}, lambda i, b: None)
    out: Set[str] = set()
    for node, binds in walk.expect:
        if node[0] == "lit":
            out.add(node[1])
            if node[1] == "group:" and word.lower().startswith("group:") \
                    and m is not None:
                out |= {f"group:{g}" for g in m.groups}
        else:
            try:
                out.update(_slot_names(node[1], root, sub, binds, word,
                                       mgr, ctx))
            except Exception:
                pass             # a name source that can't be read: skip
    if "=" not in word:
        out |= {f"{k}=" for k in walk.keys}
    return sorted(out)


def complete(text: str, mgr, ctx) -> Tuple[int, List[str]]:
    """Tab completion for one command line. `text` is the line up to the
    cursor. Returns (start, candidates): the word being completed is
    text[start:], and each candidate is a whole replacement for it that
    starts with it (case-insensitively). Candidates ending in `.`, `=` or
    `:` continue the word (no space after them)."""
    stripped = text.lstrip()
    lead = len(text) - len(stripped)
    if not stripped.startswith("!"):
        return len(text), []
    body = stripped[1:]
    if body[:1].isspace():
        return len(text), []
    words = _split_words(body)
    if words is None:
        return len(text), []
    if body and not body[-1].isspace() and words:
        word = words.pop()
    else:
        word = ""
    start = len(text) - len(word)
    if not words and start == lead:
        start += 1                      # the `!` stays
    cands = _command_candidates(words, word, mgr, ctx)
    low = word.lower()
    return start, [c for c in cands if c.lower().startswith(low)]


def common_prefix(cands: Iterable[str]) -> str:
    """The longest prefix every candidate shares (case-sensitive)."""
    cands = list(cands)
    if not cands:
        return ""
    lo, hi = min(cands), max(cands)
    i = 0
    while i < len(lo) and i < len(hi) and lo[i] == hi[i]:
        i += 1
    return lo[:i]


def apply_completion(text: str, mgr, ctx) -> Tuple[str, List[str]]:
    """Bash-style: (new text, candidates to list). One candidate replaces
    the word (plus a space unless it continues with `.` / `=` / `:`); several
    extend it to their common prefix and are returned for listing when
    nothing could be added."""
    start, cands = complete(text, mgr, ctx)
    word = text[start:]
    if not cands:
        return text, []
    if len(cands) == 1:
        c = cands[0]
        return text[:start] + c + ("" if c.endswith((".", "=", ":")) else " "), []
    pre = common_prefix(cands)
    if len(pre) > len(word):
        return text[:start] + pre, []
    return text, cands


def format_candidates(cands: List[str]) -> str:
    """A candidate listing for a log / terminal, capped at LIST_CAP."""
    shown = cands[:LIST_CAP]
    more = len(cands) - len(shown)
    return "  ".join(shown) + (f"  …and {more} more" if more > 0 else "")
