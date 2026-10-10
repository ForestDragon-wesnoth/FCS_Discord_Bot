# FCS_Discord_Bot — Notes for Claude

A Discord/CLI virtual tabletop for tactical RPGs, written in Python;
you'll be extending it. Read this whole file before doing anything
substantive — it captures hard-won knowledge from prior sessions that
the code alone won't teach you.

---

## 1. The user's design philosophy (NON-NEGOTIABLE)

The user has a sharp, consistent philosophy. Internalize this before
proposing anything:

### "What X does is stored in X"

A sword's damage lives in `vars.inventory.sword.damage`. A status's
effects live in the status's data dict. An action's body lives at
`vars.<container>.actions.<name>.body`. The engine provides
**primitives**; the GM composes mechanics from data.

### "When in doubt, gamerule"

If you find yourself coding a hardcoded constant (a default hp value,
a fixed combat formula, a specific corpse rendering, a hardcoded
"revive at full hp" rule), **stop**. The user will ask why it's not
a gamerule. Make it one. Defaults are fine; hardcoded behavior buried
in code is not.

Past sessions have repeatedly added gamerules where Claude wrote
constants. By session ~50 this was already the pattern. Look at
`RULES_REGISTRY` in `logic.py` — almost every behavior with a number
or string in it is configurable.

### NO hardcoded inventory / status / combat / damage / class systems

The engine doesn't know what an "inventory" is. It has dict-shaped
vars. It doesn't know what "damage" means — it has hp clamps and
formulas. Status data shapes are GM-defined; the engine only knows
two specific conventions (`skips_turn`, plus the user-overrideable
tick mechanism). When proposing a new feature, **ask**: "is this a
primitive (composable, generic) or a system (opinionated, specific)?"
Primitives win.

### Modular > integrated

A new feature should compose with existing primitives, not bypass
them. Adding `summon`? Make it route through `Entity.spawn` so
on_entity_spawned still fires. Adding `revive`? Make it run a
configurable effects formula, not a hardcoded heal.

### NO automated entity behavior / AI (intentional, lasting)

The engine has **no AI / behavior layer**, and the user has confirmed
this is **100% intentional and staying that way**. Every action is
GM/player-invoked or driven by GM-authored data (passives, watchers,
actions). Do NOT propose or build autonomous entity behavior —
target-selection, pathfinding-to-enemy, "the monster takes its turn by
itself." For a system this flexible, baking in AI would be
unsustainable and exponentially hard (coding game AI is a different
discipline, viable only for fully-hardcoded games). There are **no
major plans to automate entities** beyond, *at most*, the absolute most
primitive cases — a stationary turret, a very basic horde mover — and
even those should fall out of existing primitives (a passive/watcher
the GM writes), not a new AI subsystem. When a feature idea reduces to
"the engine decides what an entity does," stop and reconsider.

### Don't add features that existing primitives already compose

User rule: **we shouldn't add features that can easily be implemented by
stringing together existing functionality, unless it is something that needs
to be simplified often enough.** Before proposing a feature, try to build it
from what ships (formulas, actions, passives, watchers, macros, `!foreach`,
events). If that works in a few lines, the answer is a scenario that shows how,
not new engine code. Add a shortcut only when GMs would write the same
composition over and over (lowest_var / highest_var exist because every
targeting formula needed the same accumulator loop).

### The square grid is fundamental

The map is a grid of square cells and stays that way: every geometry
primitive, footprint, LOS walk, region, renderer and coordinate convention
assumes it. Hex grids were proposed and rejected by the user (a refactor
touching everything, harder maths and rendering, and they make hex games in
Battle for Wesnoth). Don't propose other grid shapes.

### Backwards compat is irrelevant this early

The user has said this repeatedly. **Rewrite > dual-implementation
shim.** Don't add compat hacks. If a refactor breaks an old behavior,
update the call sites and move on. (Existing scenarios may legitimately
break — fix them, don't paper over.)

---

## 2. Process discipline (the things you'll forget)

### Start every task from a fresh main

Multiple sessions burned tokens because Claude built on a stale main, got into
stacked-branch hell, and had to rebase / retarget PRs. The first command of a
new task:

```bash
git fetch origin main && git checkout main && git pull origin main && git checkout -b claude/<descriptive-name>
```

(When the session names a designated branch, recreate THAT branch from
origin/main instead.) The user merges fast: when continuing from a previous
session, check whether its PRs merged before building on them.

### ONE active PR at a time

Unless the user explicitly says otherwise, only have one PR open. A follow-up
on a feature whose PR is still open goes on that PR's branch: add the commits,
push, and update the PR's title/body to cover the combined scope. If you had
started a second branch, close its PR and delete the branch. The user has
corrected this twice.

### Verify the PR is still open after every push

Pushing to a feature branch does NOT extend an already-merged PR: follow-up
commits land on a dead branch, show up nowhere, and the user only notices from
GitHub's "Compare & pull request" banner. After every push, check
`git fetch && git log origin/main` (or the PR state via the MCP tool). If it
merged, restart the branch from main and open a new PR.

### Run the regression after every meaningful change

```bash
python run_scenarios.py
```

Run it after every commit-worthy change and fix a break before adding more
code. How the harness works (`run_scenarios.py`, scenarios in
`test_sequences.txt`):

- **Format.** Every `SCENARIO N — title` header needs a BLANK LINE before it
  and a dashes line after it (a header missing the blank line used to be
  swallowed into the previous scenario's prose and never run; the harness now
  cross-checks header lines against what it parsed and refuses to run on a
  mismatch or a duplicate number). Only lines ABOVE a scenario's first
  `Expected:` run: a staged scenario labels intermediate prose `Result:`
  (see 13, 26). Multi-line bodies use literal `\n` (`_interpret_escapes`).
- **What it flags.** `💥` and "Syntax error" always; `❌ Runtime error:`,
  `❌ Unexpected error:`, an unknown subcommand ("isn't a valid command") and
  missing arguments ("is missing arguments") unless the prose carries
  `HARNESS-ALLOWS-ERRORS`; a false `!assert "<formula>"` (`❌ Assertion
  failed`) and a missed `?? <text>` / `?! <text>` reply check (`❌ Expectation
  failed`) ALWAYS (only `HARNESS-ALLOWS-ASSERT-FAIL` opts out, for a scenario
  demonstrating a failing assert).
- **What it doesn't.** Anything else. A scenario "passes" with a `❌` reply
  that means the opposite of what it should, so new scenarios check their end
  state with `!assert` (see 597-608) and their reply wording with `??` / `?!`
  lines right after the command (see 633-645).
- **Reading replies.** `python run_scenarios.py --review N ...` prints every
  command's full reply next to the Expected prose; `-v` prints a transcript.
- **Isolation.** Each scenario gets its own temp data folder (real storage
  commits) and a temp saves folder; `!as restart` reloads everything from
  disk as a bot restart would.

### Don't trust your own scenario expected-text

You'll write Expected prose from your mental model and sometimes be wrong (the
user has caught off-by-one round counts more than once). Run the scenarios and
read the actual replies before declaring done; the prose is documentation of
what shipped, so fix the prose when reality differs.

### How many scenarios per PR

The user enforces this (a prior session shipped many PRs with one scenario
each, which under-tested complex features):

- **Simple PR** (one small primitive, one rule): **1-2** scenarios.
- **Complex PR** (a subsystem, several sub-features or interacting knobs):
  **3+**, one per distinct behavior plus a failure/edge case.

Each scenario exercises a DIFFERENT facet, and at least one proves the feature
in REAL USE (a formula/action consuming the new primitive end-to-end), not
just that the setter runs. Follow-up tests for an older feature go right after
that feature's scenarios, renumbering the rest; update the scenario numbers
cited in this file to match.

### ALWAYS smoke-test new features on a multi-tile entity

Multi-tile / footprint entities and their cousins (body parts, riders,
anchored auras, segments) are the single biggest source of interaction bugs:
almost every audit fix traced back to code that assumed a 1×1, anchor-only
entity (anchor-only bounds or occupancy, sight cast from the anchor cell, a
hook or clock that skipped attached parts, a carry that moved the anchor but
not the footprint). **Any new feature is exercised at least once against a
multi-tile entity (`footprint_w`/`footprint_h` > 1, a body part, a rider or a
segment) before it's done** — a throwaway repro or a `-v` transcript is fine.
The questions: does it measure / membership-test by ANY covered cell? Does it
validate the WHOLE swept footprint? Does it carry the whole body on move? Is an
attached part included where it should be, and excluded where it shouldn't?

### Rendering changes need a GUI-vs-ASCII pass (user directive)

Text (`_render_ascii_impl`) and graphics (`_render_scene_impl` →
`sprite_render.SceneRenderer`) are two parallel renderers over the same
predicates, and they drift. After ANY change to what a map shows (a layer, a
visibility rule, a glyph / sprite / colour resolver, the viewport, a new kind
of placement), compare the two paths side by side and run the parity
harnesses (§5 "Audit toolkit") over random boards with multi-tile bodies,
region parts, riders, every facing, team views, hidden layers and viewports.
The intended differences are listed in §7 "Rendering".

### Every bug is in scope; multi-tile is where they cluster

The multi-tile emphasis says where bugs concentrate. Fix any defect you find,
whatever it touches (the shallow-copy undo corruption, for one, had nothing to
do with footprints). Two corollaries the user stated:
- **If a fix's intended behavior is ambiguous, ASK.** A wrong "fix" that
  drifts from intent is worse than a question.
- **If this file's wording was ambiguous about the behavior, amend it** as
  part of the fix.

### Checklists: when you add X, also do Y

Each of these was learned from a shipped bug.

- **A formula function:** add it to `_MATCH_FUNC_NAMES` AND the namespace
  builder (or `_ALLOWED_FUNCS` for a pure helper); classify it in
  `ARG_SAFE_MATCH_FUNCS` (reads / calculates) or `ARG_MUTATING_MATCH_FUNCS`
  (changes state — banned from `$()` args; when unsure, MUTATING). The
  module-load drift guard fails the build if you forget, but decide
  deliberately: a mis-classified mutator lets a player `$(kill(boss))`. Add a
  `$()` scenario: a read-only happy path and a security case proving a mutator
  is rejected and changes nothing (see 541-543). A mutator calls
  `match.surface_log(<log>)` so its hook output reaches chat. A function whose
  cost follows its arguments clips to the map or charges `formula_cell_limit`
  / `formula_loop_limit` / `sight_check_limit`. Loopable → `_LOOPABLE_FUNCS`.
  Exercise every new function: import-time checks prove only registration (a
  nonexistent helper name once hid in a function body).
- **A HOOK_CONTEXT binding:** `_who_arg` handles HOOK_CONTEXT_NAMES by set
  membership, so `entity[<binding>]` works automatically; check the binding is
  actually supplied in the fire site's extras.
- **A gamerule:** schema keys — int rules carry `min` / `max` / `unlimited`
  (-1), str rules with fixed words carry `choices` (enum), formula-valued
  rules carry `"formula": "expression"|"program"`; a limit a server could
  raise high enough to stall the bot carries a ceiling (`"ceiling": True`,
  §3). Read it with the exact registry name (a typo'd `rules.get` returns the
  default forever).
- **A serialized Match field:** `to_dict` / `from_dict` with deep copies on
  BOTH sides; a runtime-only field goes in `action._rollback_match`'s preserve
  list (`_check_rollback_fields` raises if a field is neither); decide whether
  undo restores it (`_NOT_UNDO_STATE`, `_restore_snapshot`) and whether
  `resize_grid` / `copy_entity` must shift or remap it.
- **A field or var that names another entity by id:** every path that copies
  or moves an entity across a boundary (corpse, clone, transfer/copy,
  transform, template save/spawn, revive) must remap or strip it (§5).
- **A command / subcommand:** an exact `annotate_sub` usage (the stray-word
  check reads it); alias spellings in `_SUB_ALIASES`; player-available reads
  in `READ_ONLY_SUBCOMMANDS[root]`; a host-only READ in `_HOST_READS` (or a
  pause with `pause_affects_hosts` holds it); a read form of an elevated arg
  in `_ELEVATED_READ_FORMS`; anything touching server-wide state or host files
  in `_admin_required` (files via `saves_path`); naming another match →
  `require_target_host`; a raw (un-shlexed) tail → `split_command_args`;
  `int()` on user input wrapped to a clean ❌; an optional slot that takes
  any word refuses a word that doesn't fit; a body-accepting command calls
  `normalize_body_source`.
- **A query that names or lists units:** run it through the POV helpers
  (`_query_eid`, `_pov_hides`) so a fogged player can't probe hidden units;
  re-run the POV leak detector (§5).
- **Anything iterating units and firing hooks:** re-check
  `eid in m.entities` after each hook (§5 "ghost firing").

### Usage strings are authoritative (stray-word check)

The dispatcher refuses positional words past the end of a command's usage
string (`_stray_words_error`): `!ent remove a b` used to remove only `a`. Usage
grammar: `<x>` one word, `[...]` optional, `|` alternatives (an alternative LED
by a literal word applies only when that word is typed), `...` / `<x ...>` any
number more, `key=<v>` / `[key=value ...]` options (left to the handler). A
wrong usage string refuses a valid command; the regression catches most.

### Commit messages: dense, factual, no fluff

Match the existing messages on main: why the change was made, the mechanism,
known trade-offs. No "Closes #N", emoji or marketing language.

### MCP tools reconnect

`mcp__github__*` tools drop and reconnect often. After a notice that they're
back, load the schema with `ToolSearch` before calling
(`mcp__github__create_pull_request` / `update_pull_request` are the usual two).

---

## 3. Codebase map

### Files (with role)

- **`logic.py`**: the domain model — `Entity`, `Match`, `MatchManager`,
  `Workspaces`, `Passive`, `ClampSpec`, `SpecialTileTemplate`,
  `FormulaFunction`, `GameSystem`, the store helpers (`_own_value`,
  `check_store_path`, `checked_unit_vars`, ...) and the template helpers.
  **`RULES_REGISTRY`** at the top is the single source of truth for every
  gamerule; its `desc` strings are the detailed documentation of each rule
  (searchable in play with `!help find <words>`), so this file names rules
  without repeating them. **`HOOK_NAMES`** is the registered event surface.
- **`formula.py`**: the sandboxed expression/program language (§4) and every
  formula function. `_MATCH_FUNC_NAMES` (match-bound) / `_ALLOWED_FUNCS` (pure)
  / `_LOOPABLE_FUNCS` / `ARG_SAFE_MATCH_FUNCS` / `ARG_MUTATING_MATCH_FUNCS`.
- **`vtt_commands.py`**: the command surface. `CommandRegistry` (every
  `!command` is a `@registry.command` handler; `annotate_sub` documents
  subcommands), the dispatcher and access gate, and the shared command-line
  splitter (`split_command_line` / `split_command_args`) every surface uses.
- **`action.py`**: actions — `Action`, `discover_actions`, the runtime
  bindings (`SourceProxy` / `ArgsProxy` / `Coord`), `run_action` (the
  transactional runner), `_rollback_match` (restores a match in place; also
  used by spawns, `!batch strict` and storage rollback), `_BufferCtx` +
  `_sync_dispatch` (the sync formula engine dispatching async commands).
- **`match_history.py`**: undo snapshots (round / turn / command / manual).
  `Snapshot.state` is `Match.to_dict(include_history=False)`, read lazily
  from disk once persisted.
- **`storage.py`**: the data folder on disk (§3 "Persistence").
- **`sprite_render.py`**: the surface-agnostic Pillow renderer
  (`SpriteLoader`, `SceneRenderer`, `render_scene_png`, `fit_cell_size`).
- **Surfaces:** `bot.py` + `discord_commands.py` (Discord: per-server
  routing, approval / resume buttons, auto-update boards, image posts),
  `cli.py` (REPL), `gui.py` (tkinter canvas over the scene model),
  `run_scenarios.py` (the harness). All of them are live code that features
  touch.
- **Surface capabilities are optional ctx attributes**, read with
  `getattr`, so the engine stays surface-agnostic and the harness exercises
  the fallback: `supports_color`, `viewport_capable`, `message_limit`,
  `auto_approve`, `post_scene_image`, `show_scene_view`, `set_autoupdate`,
  `offer_resume`, `prompt_choice`, `retire_server_boards`. The Discord
  adapter also routes `!<alias>` through `on_command_error`, never pings
  (`AllowedMentions.none()`), splits long replies fence-aware, and retires a
  board whose channel was unbound.
- **`test_sequences.txt`**: the scenarios (§2).
- **`docs/audit_history.md`**: the narratives of audit passes 2-36 — what was
  swept, verified clean, measured. Read the relevant passes before auditing
  an area again.

### The cross-cutting chokepoints (memorize these)

| Chokepoint | What flows through | What hooks in |
|---|---|---|
| `Entity.write_var` / `remove_var` | every var write / delete | store checks (vitals, reserved paths, footprint fit, partial-template markers), clamps, partial-template change tracking, var hooks (`on_var_*`), death check (top level only) |
| `Match._emit_status_diff` | every status change | status hooks (`on_status_*`), death check |
| `CommandRegistry.run` → `_run_top` / `_run` | every typed `!command` | command lock, alias resolution, access / admin gate, pause, inline `$()`, undo snapshot, formula log sink, `CURRENT_WORKSPACE`, watcher poll, storage commit |
| `dispatch_no_snapshot` | batch / run / macro / foreach lines, action `cmd()` | admin check, inline `$()`, `!assert` stop sentinel, no gate (reached only from an already-gated command) |
| `action.run_action` | every action | pre-state for rollback, `_action_depth`, choice replay, on_action_used / failed / on_target |
| `Match.summon_entity` / `Entity.spawn` | every dynamic creation | id minting, placement, `default_entity_vars`, `checked_unit_vars`, on_entity_spawned, summon budget |
| `Entity.remove` | every removal | part subtree, riders, auras, schedules, turn pointer (`turn_vacated`), on_entity_despawned |
| `Match._process_death` | every death | on_death, corpse vs delete, part death routing, turn rebuild |
| `Match.fire_entity_moved` | every completed move | part / aura / rider re-stamp, snake resettle, fog memory |
| `Match.surface_log` | log lines from formula-driven changes | the action buffer or the command's `FORMULA_LOG_SINK` |
| `Storage.commit` | after every top-level command | disk mirror, limits, autosave / old-template cuts, rollback |

Add cross-cutting features at these points, not at the call sites.

### Server workspaces (each Discord server is separate)

ONE bot process serves every Discord server it's in, and each server has its
own WORKSPACE: a `MatchManager` holding that server's GameSystems (rules,
`command_access`, default vars / passives / clamps, system aliases), matches,
channel pointers, saves folder and command lock. `logic.Workspaces` maps a
guild id to its manager, creating it (with a fresh `default` system built from
RULES_REGISTRY) on the server's first command. The Discord adapter
(`discord_commands.wire_commands(bot, workspaces)`) routes every command,
alias, button click and board refresh by the guild the message came from, so
a match id, system name or save name only means something inside its own
server, and nothing can name another server's data. The CLI / GUI / harness
use the `local` workspace (`logic.LOCAL_WORKSPACE`); the CLI and the harness
switch workspaces with `!as server <key>` (scenarios 698-700).
- **DMs are refused** (`DM_REFUSAL`: make a one-person server instead) — a
  DM has no server to hold a workspace. An unknown `!word` in a DM is ignored.
- **Server-wide commands need a server administrator** — `!system` edits,
  `!defvar`/`!defpassive`/`!gclamp` edits, `!log format`, `!store`, `!run`,
  `!history export/import`, `!server wipe` (`vtt_commands._admin_required` + `ctx_is_admin`; Discord =
  the guild Administrator permission, CLI/GUI = always). They change state
  shared by every match of the server, and anyone can create and host a
  match, so a match host isn't enough. Checked in BOTH `CommandRegistry.run`
  and `dispatch_no_snapshot` (so batch/macro/foreach/`!run` lines and action
  `cmd()` can't wrap them), rejected outright (never queued), not overridable
  by `!host access` / `command_access`, and it skips the per-match gate once
  passed. Action bodies can't run them at all (`_BufferCtx` carries no admin
  flag — default-deny).
- **Commands that NAME another match** (`!match use/bind/rename/delete <id>`,
  `!ent copy/transfer <id> <dest>`) need host (delete: owner) of THAT match
  (`require_target_host`) — the access gate only checks the CHANNEL's match,
  and one server can run several matches with different hosts.
- **Bot owner.** `Workspaces.owner_ids` is read at startup from Discord
  (`bot.application_info()`: the application's owner, or its team's members);
  `ctx_is_bot_owner` (Discord sets `is_bot_owner`; CLI/GUI = always; default-
  deny elsewhere). Owner-only commands live under `!owner` (`!owner servers`
  lists every workspace); `!whoami` shows the badge. Owner is NOT admin of
  every server: server-wide commands still need that server's Administrator.
- **System rules on load:** a system saved by an older build may name a
  gamerule that no longer exists — `GameSystem.from_dict` drops it with a
  console note. A gamerule the save lacks needs nothing: systems store only
  overrides, so it reads the RULES_REGISTRY default.
- `!system default server|channel` (the old `global` scope and the per-server
  default map are gone — a workspace is one server). `!match` / `!match list`
  are player-available: they list only this server's matches.
- STILL SHARED across servers: the process and its event loop. A slow command
  in one server delays every server, so anything whose cost follows a user's
  numbers needs a budget. The limit rules a server could raise high enough to
  stall everyone carry a CEILING (schema `"max"` + `"ceiling": True`; the bot
  owner changes it with `!owner ceiling`, kept in `logic.RULE_CEILINGS` and
  bot_settings.json; `_build_rules_dict` clamps values above it). Sight lines
  and area builders are CLIPPED to the map before they are walked
  (`Match._line_span`, `formula._clip_range` / `_bresenham_on_map`), so their
  cost follows the map, not the arguments. A NEW limit rule a server can set
  needs a ceiling; a NEW function whose cost follows its arguments needs
  clipping or a budget.

### Persistence: the data folder (storage.py)

Everything persists to `data/` (`storage.DATA_DIR_DEFAULT`, git-ignored),
one folder per server (`local` for the CLI / GUI):
`workspace.json` (channel pointers, default system, per-channel defaults),
`systems.json`, `matches/<id>.json`, `matches/<id>/history/` (`index.json` +
one `<sequence>.json` per undo snapshot), `saves/`, `sprites/`, `templates/`
(entity templates, outside commits; `templates/.trash/` = their replaced /
edited / deleted versions, 24 h), `corrupt/`;
plus `data/bot_settings.json` (owner limits + rule ceilings) and
`data/.trash/` (wiped data, 24 h). Mechanics:
- **Commit after every top-level command** (`vtt_commands.persist_workspace`,
  called from `CommandRegistry.run` inside the command lock, also after a
  Discord pan click). `Storage.commit(mgr)` serializes every match and the
  workspace / systems and rewrites only files whose text changed (compared
  with `_written`, the text last written); removed matches / snapshots are
  deleted. Each file is atomic (temp + rename); no journal (user-accepted:
  a crash can leave two files of one command a command apart).
- **Match files and snapshot files leave out `rules`** (`_disk_state`): it
  was 87% of an empty match file and is rebuilt from the system on every load
  / restore. A snapshot read back from disk therefore has no `rules`; the
  snapshot diff only compares rules when both sides have them.
- **Lazy history:** `Snapshot.state` is a property; a persisted snapshot
  drops its state from memory after the commit (`release_state`) and reads
  `path` on first use. `MatchHistory.index()` / `from_index()` are the disk
  shape. `persist_undo_history` (rule, default on) = history files written;
  off = memory only, files deleted.
- **Limits** (`Storage._over_limit`): per server (default 1000 MB, the whole
  server folder) and global (20 GB, everything incl. trash), owner-set with
  `!owner limit`. A commit that GROWS past a limit first cuts the server's
  old template versions (`templates/.trash`), then its oldest autosaves
  (never manual saves) from matches with
  `storage_trim_autosaves` on (now OR at the last commit, so turning it off
  works over the limit); if that can't make room, `Storage.rollback` puts
  the server back to what's on disk — matches in place via
  `action._rollback_match`, history from the index — and the user is warned.
  A commit that doesn't grow is always allowed. `!store save` / `!history
  export` check room first (`check_room`) and are refused if over.
- **Startup:** `storage.open_workspaces()` (bot.py, cli.py, gui.py) loads every
  server folder; a file that fails to load moves to `corrupt/` with a console
  note and the rest loads; unknown gamerules in systems are dropped. Data of
  servers the bot left is kept. On shutdown `commit_all` writes once more.
- **cli.py / gui.py persist only when asked (user call).** They read
  `local_settings.json` beside the code (`storage.load_local_settings`;
  created on first start with `"match_persistence": false` and a `_comment`
  list explaining it — JSON has no comments; git-ignored; the key was
  `persistence` until templates shipped, an old file's key still works with
  a rename warning). One switch for both.
  Off = `open_workspaces(persist=False)`: `Storage.persist` False makes
  load_all load no match / system / workspace data and every commit a
  no-op, so each session starts empty; whatever is in data/local is IGNORED
  AND LEFT ON DISK (turning it back on loads it). Entity TEMPLATES ignore
  the switch (user call): always written as saved and loaded at start.
  Manual saves still go to data/local/saves and data/local/sprites is still
  searched. `!server wipe` is refused
  (`NO_PERSIST_WIPE`: it would act on the ignored disk state); `!as restart`
  gives an empty session. A startup warning (`local_persistence_note`) is
  printed by both and shown in the GUI log. The Discord bot always persists;
  the harness uses its own temp Storage (persist on).
- **`!server wipe matches|all`** (admin; refused inside batch / macro / alias
  / action / `!again`: `_SERVER_TYPED_ONLY`): prints a phrase, accepted from the
  same admin in the same channel within 2 minutes (case / spaces ignored);
  `Storage.move_to_trash`; channels then point at no match and Discord boards
  are retired (`retire_server_boards`). `!server wipe undo` restores the
  newest trash entry within 24 h, refused while anything made since exists
  (`_wipe_blockers` lists it). `!server storage` shows use vs limit.
- **Tests:** the scenario harness gives every scenario its own temp data
  folder, so every command runs through the real commit / rollback; `!as
  restart` (CLI / harness, `storage.reload_workspaces`) reloads everything
  from disk as a bot restart would — use it to prove something persists.
- Undo history disk growth is a tracked concern (§8 "Deferred").

### Disk access is confined to the server's saves folder

Every command that reads or writes a host file — `!store save/load`, `!run`,
`!history export/import` — goes through `vtt_commands.saves_path(name, mgr,
write=)`, which resolves plain relative names (subfolders allowed) inside the
workspace's folder (`server_saves_dir`: with storage `data/<guild id>/saves/`;
without it `SAVES_DIR` for `local` and `SAVES_DIR/servers/<guild id>/` for a
Discord server, so one server's `!store load` can't read another's files)
and refuses `..`, absolute paths,
drive letters and anything whose realpath (symlinks included) leaves the
folder. Replies show `saves/<name>`, never the host's absolute path. Before
this, `!run 1bot_token.txt` echoed the bot token back line by line ("Unknown
command `<token>`") to ANY user, and `!store save` could overwrite any file.
`saves/` is git-ignored. The scenario harness points `SAVES_DIR` at a temp
folder for the run (never touch a real campaign folder). If you add a new
file-touching command, use `saves_path` AND list it in `_admin_required`.

---

---

## 4. The formula sandbox — what you can and can't do

**Allowed AST nodes** (`_ALLOWED_NODES`): Module, Expression, Expr, Assign,
If, Pass, For, Tuple, BinOp, UnaryOp, BoolOp, IfExp, Compare, Call, keyword,
Attribute, Dict, List, Name, Constant, Load, Store, arithmetic and comparison
operators (incl. In/NotIn), And, Or. **Banned:** Subscript (except
`entity[X]`), Lambda, While, comprehensions, Import, Try, With, class/function
defs, AugAssign, chained/tuple Assign, loop `break`. A Call's function must be
a bare Name.

**Two modes.** EXPRESSION formulas (conditions: block / opaque / visibility /
alive / death conditions, watcher conditions, slot conditions, modifier
values, `$()` args) can't assign. PROGRAMS (passives, ticks, watcher effects,
`!func` bodies, actions) can. A rule's schema says which (`"formula":
"expression"|"program"`), and `!system set` validates it.

**Identifier surfaces** resolved at runtime: `_ALLOWED_FUNCS` (pure helpers),
`_MATCH_FUNC_NAMES` (match-bound), `HOOK_CONTEXT_NAMES` (bindings a fire site
supplies in EvalCtx.extras: actor, target, args, changed_key, pov_team,
tile_x, corpse_id, vehicle, slot, skip_status, event_name, ...),
`_ENTITY_TOKEN_NAMES` (self / this / current, and `parent` for a body part),
`known_funcs` (the match's `!func`s), `known_params` (loop variables, and
locals in action mode).

**`entity[X].path`** reads and writes entity vars. X is a literal id, a token,
a known param, an action binding or any HOOK_CONTEXT name; any OTHER bare Name
is read as a LITERAL id (`entity[foo]` = the unit with id "foo"). The path side
has reserved names (`logic.RESERVED_VAR_PATHS`): `x`, `y` and `name` read the
unit's position / display name, and nothing can be written at or under them.

**Loops.** `for v in <call>` iterates only a `_LOOPABLE_FUNCS` function
(`entities_within`, `parts`, `range`, `each`, `keys`, `team_members`, ...);
`range(...)` gives counted loops and `each(value)` iterates a list or a dict's
keys. No `break`: guard the body with a flag, or use a capped helper
(`entities_in_line_until`, `lowest_var` / `highest_var`). Iterations are
bounded by `formula_loop_limit`.

**Values.** Coordinates are LISTS (`[3, 3]`): built-in results are listified
(`_listify`), a tuple literal compiles to a list, stored tuples become lists;
read them with `coord_x` / `coord_y` or `get(c, 0)`. `get(container, key)`
reads a list index / dict key / dotted path (subscripts are banned). Stored
values are deep copies with string dict keys (`_own_value`), and only JSON
values (numbers, text, bools, None, lists, dicts) can be stored.

**Resource bounds.** `_ArithGuardTransformer` rewrites `** * + %` into bounded
helpers before every compile (huge ints, huge strings / lists refused;
`formula_size_limit`); geometry builders charge `formula_cell_limit`, sight
lines `_check_line_budget`; `!func` recursion has its own limit. One bot
process serves every server, so a formula's cost must follow the map, never
its arguments.

**Action mode** (`action_mode=True`) adds: bare-name assignments become
locals (the validator pre-collects assign targets, so later references
validate); `source.<path>`, `args.<key>`, `target.x` etc. (any bare-Name root)
pass through to runtime attribute resolution — Python dunders (`__x__`) are
rejected there, engine dunder vars (`__follows`) are fine; `for x in target`
when target is a list; the builtins `cmd`, `fail`, `choose`, `choose_number`.

**Body sources** from chat contain a literal backslash-n: every body-accepting
command calls `formula.normalize_body_source` (the harness un-escapes earlier,
so scenarios mask a missing call — test with raw shlex input).

**Runtime guards** (all restored in `finally`): `_var_event_depth` (var-hook
recursion), `_event_depth` / `_event_stack` (the event bus), `_action_depth`
(also suspends per-command snapshots), `_death_processing`,
`_alive_eval_depth`, `MatchManager._macro_depth`, `_summon_count` (per-command
summon budget).

---

## 5. Bug classes, traps and the audit toolkit

Every class below shipped at least once and was fixed in an audit pass
(details per pass in `docs/audit_history.md`). Check new code against them.

### Bug classes

- **Aliasing (shared mutable objects).** A snapshot, a stored value or a
  loaded match that shares a dict / list with live state gets corrupted by a
  later in-place write: undo restored mutated values, a second restore of the
  same snapshot differed, `entity[a].inv = entity[b].inv` made two units share
  one inventory. Rules: `to_dict` AND `from_dict` deep-copy every nested field
  (vars, statuses, tiles, zones, turn_order, rules, ...); every store goes
  through `_own_value`. When live state looks right but a restored one
  doesn't, suspect aliasing before the writer. Also: an undo / restore REPLACES
  the Match object in `mgr.matches`, so re-fetch it after one.
- **Ghost firing (a unit removed mid-operation).** A hook or tick that kills
  or removes the unit, after which the code keeps acting on it: own passives
  firing after a global handler removed the unit, the remaining statuses of a
  unit killed by its first tick, a mover walking on after a lava tile killed
  it, a damage share routed to a part the cascade already removed, a reply
  looking up a unit that just died (💥). Rule: after any hook-firing call,
  re-check `eid in m.entities` before touching the unit again; guard
  `self._match is not None` after a mutation that can trigger death.
- **Dangling relational ids.** Units reference each other by bare id
  (`part_of`, `mounted_on` / `mount_slot`, `__follows`, a zone's `anchor`,
  schedules). An id is the engine's only identity, so a stale one silently
  latches onto whatever later takes that id. Every path that copies or moves
  a unit across a boundary (corpse, clone, transfer / copy, transform, template
  save / spawn, revive) remaps or strips these; removing a unit removes its
  part subtree and closes snake chains.
- **Anchor-only assumptions.** See §2 "smoke-test on a multi-tile entity".
- **Round-keyed logic under ATB.** ATB disables rounds (`round_number` stays
  1), so anything windowed or expiring by round silently never advances.
  Key durations on `turns_elapsed` (counts in both turn models) or record
  which clock a deadline used.
- **Work that follows user numbers.** One process serves every server: a
  loop, rectangle, line, repeat or recursion sized by an argument stalls the
  whole bot. Clip to the map while building (`_grid_box`, `_line_span`,
  `_clip_range`), charge a limit rule, give the rule a ceiling. A runaway must
  also count iterations that dispatch nothing (an empty nested `repeat` once
  bypassed `macro_step_limit`).
- **Indirect recursion.** `!macro run` inside a macro, a `!run` file running
  itself, `!again` inside a batch rerunning its container, an alias of `again`:
  each recursed to Python's stack limit. Each indirection has a depth guard or
  a refusal.
- **Silent fallbacks.** A typo'd mode read as the default (a misspelled
  metric became Chebyshev, an unknown damage mode split by weight), an unknown
  `key=` option or a stray word dropped, a rule value stored unchecked, a
  rule defined but read nowhere. Refuse unknown words, validate rule values
  (schema min / max / choices / formula), and make fail-open conditions
  report their errors where a GM looks (`!map cell ... for=`).
- **Dropped output.** A Match method returns its hook log and the caller
  threw it away, so passives fired silently. Formula mutators call
  `surface_log`; command handlers append the log of every write they make.
- **Rollback / undo vs runtime state.** `_rollback_match` copies serialized
  state back and preserves an explicit list of runtime fields (event stack,
  summon budget, pending requests, held commands, undo history, ...); a
  stale list once wiped live emit frames and all undo history on any failed
  action. `_restore_snapshot` carries the approval queue and held commands,
  keeps live bindings per `undo_channel_bindings_mode`, keeps `paused`, and
  re-copies `rules` from the system.
- **Discord concurrency.** Each message is its own asyncio task and handlers
  await their replies, so commands interleaved: one command's changes landed
  in another's undo step. `CommandRegistry.run` holds a per-server lock
  (re-entrant within a task via `_HOLDS_COMMAND_LOCK`). Per-command state
  lives in context variables (`FORMULA_LOG_SINK`, `CURRENT_WORKSPACE`,
  `_RUN_DEPTH`, `_AGAIN_ACTIVE`), never in a per-match list another task
  could drain. Rendering in a worker thread gets a scene built on the loop
  (`scene_for_png`) and only draws pixels.
- **POV leaks.** Any reply naming units, positions, vars or events can leak
  what a fogged team shouldn't see: listings, `!ent info`, `!dist`, `!find`,
  diffs, logs, camera centering, corpses inside tile data. See §7 "Access,
  POV".
- **Non-string joins.** `", ".join(...)` / an `re.sub` replacement fed a
  number from a var (a numeric team, an int dict key) 💥'd. `str()` values
  from vars before joining.
- **Function-local imports shadow module names.** `from formula import
  FormulaError` deep inside a handler makes `FormulaError` local to the
  WHOLE function, so an earlier `except FormulaError` raised
  UnboundLocalError. Import at module level (an `as` alias is safe).
- **Non-JSON values.** A `type` object reached via a dunder, or a function
  used as a value, stored in a var made the match unsaveable. Stores refuse
  anything but JSON values; action mode rejects dunder attributes.

### Other traps

- **Action `cmd()` and async.** The formula engine is synchronous and a real
  Discord `send()` awaits; `_BufferCtx` collects output during the body and
  the top-level `run_action` flushes it afterwards. Don't drive coroutines
  with `.send(None)`.
- **The harness masks escape handling** (§4 "Body sources").
- **Harness-author mistakes that looked like engine bugs:** there is no `!ent
  damage` (use `!ent hp x -5`; a bare `!ent hp x 5` SETS 5); `!passive add`
  takes `target=` / `scope=` BEFORE the quoted formula; `!ent move <id> <n>
  <dir>` (count first) but `!ent push <id> <dir> [n]`; the default soft clamp
  caps hp at max_hp, which defaults to the spawn hp; spawning onto an occupied
  cell fails (so a later mount has no rider); transform needs a CAPTURED
  statblock; coordinates are 1-based; `nearest_entity` takes the relation
  SECOND, `entities_within` FOURTH. Write probes to a FILE: `bash -c "python
  -c ..."` mangles nested quotes.

### Do audits yourself

User directive: for correctness / interaction audits, do the work directly —
read the subsystems end to end, trace the cross-cutting paths, write numeric
and behavioral assertion harnesses. Survey subagents on weaker models
reported "clean" while the bugs that mattered came from by-hand work, and
their findings were ~30% false positives. More results per pass beats cheaper
passes. Use subagents only for mechanical, local lookups, and verify every
claim an agent makes against the code before acting on it or repeating it.
The freshest code is the highest-yield target.

### Audit toolkit

Throwaway harnesses that found real bugs; rebuild them from these
descriptions (pass numbers point at `docs/audit_history.md`):

- **Stateful chaos** (28, 30): ~70 command shapes on a rich board (multi-tile,
  parts, segments, mounts, auras, statuses, fog, two matches, host + a
  red-POV player, undo with bindings, transfers, ATB toggling, disguises),
  asserting after EVERY step: no dangling ids, coordinates in bounds,
  turn cursor in range, guards / vision memo / event stack at rest, JSON round
  trip idempotent, manual saves intact. Wrap every handler to print the
  traceback of a non-VTTError. HOSTILE mode adds lethal global passives, lava
  tiles and killing zones.
- **POV leak detector** (30, 33): a fogged board with distinctively named
  hidden things (part, hidden rider, corpse, action, status, schedule, zone,
  tile, team data); every root × subcommand (`registry._help`) × an argument
  pool run as a red-POV player; replies grepped for the hidden names, values
  and cells after stripping echoes of the typed args. Include list KINDS
  (`commands`, `turns`) in the pool.
- **Formula-function fuzzer** (30): every function × wrong-typed /
  missing-entity / wrong-arity args under a 1 s SIGALRM; flag raw Python
  messages after "Runtime error:".
- **Command fuzzer** (30): every root × subcommand × junk / ids / numbers /
  paths / `$()` as host (~100k runs), flag 💥 / Runtime / timeouts; point
  `vtt_commands.SAVES_DIR` at a temp dir first.
- **Huge-number timing fuzzer** (34): 10⁹ / -10⁹ in every numeric position of
  every command and every state-changing function, under SIGALRM.
- **Reload differential** (34, 35): the same random commands on a live
  manager and on a save/load copy taken mid-game, comparing every reply and
  the full state each step (exclude the runtime-only queues).
- **Memory-equals-disk chaos** (storage work): reload the data folder into
  fresh workspaces after every command and compare every match, history index
  and snapshot state, channel pointers and systems.
- **Command-vs-formula parity** (35): each operation as `!command` and as its
  formula function on identical boards; compare end states.
- **Model-based undo** (30): random mutations + `undo command N`, checking each
  lands on the state recorded N commands back, under several retention caps.
- **Store fuzzer** (36): every read-only function's result written into a var,
  then JSON round trip and equality.
- **Stray-word detector** (36): every scenario command re-run with an extra
  word; report replies that aren't errors and don't mention it.
- **Renderer parity** (graphics passes): per-cell ownership (ASCII glyph vs
  top scene placement), pixel parity (glyph pixels where ASCII shows a
  non-`.`, fog where it shows the fog glyph), windowed render == crop of the
  full render.
- **Registry-vs-code checks** (28): every literal `rules.get("X")` exists in
  RULES_REGISTRY; every `_MATCH_FUNC_NAMES` entry is registered and callable;
  the ARG_SAFE / ARG_MUTATING sets partition the match functions.
- **Statistics for RNG primitives** (28): sample thousands of
  `hit_location` / `roll_table` picks and compare to the weights.
- **The suite as a detector** (31): list every `❌` / `⚠️` reply and every
  "X → Y" figure in the Expected prose and check each against its scenario;
  scenarios that pass while testing nothing hide real bugs.
- `pyflakes` after edits to error paths (catches NameErrors in except clauses).

---

## 6. Working with the user

### Designing a new feature

1. Fresh branch from main (§2).
2. **Ask the design questions before coding** (below).
3. Find the chokepoints / primitives you'll extend (§3) and read the
   surrounding 50-100 lines, so your code matches the local conventions
   (comment density, helper naming, error message wording).
4. Land a change of 300+ lines in small commits, each passing the regression.
5. Scenarios: the happy path AND a failure / edge case for each new behavior,
   with `!assert` / `??` checks (§2).
6. Run the harness, read the replies (`--review`), write assertion scripts
   for hp deltas, var values and error messages, and do the multi-tile smoke
   test.
7. Dense commit message; push; open the PR with a summary, the mechanism, the
   new surface, a test plan with the regression count, and notes on known
   limitations and follow-ups. Update this file (§7, §8) in the same PR.

### Asking questions

The user has said it explicitly: more questions beat an implementation that
drifted from intent. If a feature is ambiguous in shape, signature, behavior,
edge cases, naming or scope, ask. They answer fast and read good questions as
care; they're unhappy having to ask "why does this do X instead of Y?" after a
merge. Skip questions whose answer is obviously settled.

- **Bounded choices** (which mode, which stat, which enable mechanism): use
  `AskUserQuestion` with the recommended option first and labelled.
- **Open-ended direction** ("which idea next", a menu of features): ask in
  PLAIN PROSE, never the questionnaire — each idea needs room, and the user
  answers in their own words (directive, restated more than once).
- **Read free-text answers fully:** they often add requirements beyond the
  options (the template version trash came that way). Confirm the full list
  before building when an answer reshaped the plan.

**Ordering questions against the work** (directive — the user checks in only
occasionally, so a question asked mid-work idles the session until they
return):
- **Bugfix / audit passes:** fix everything with a clear answer FIRST, then
  ask the open questions together at the end.
- **Features:** as many questions as possible UPFRONT, then build everything
  certain, and save questions that come up during the work for the end. Ask
  mid-way only when the answer blocks development or testing.

### Communicating

- **Short, direct messages** with content over ceremony. Lead with the
  regression count ("722/722") and the most important behavioral change.
- **Never claim something works without verifying it.** The user catches
  sloppy work. Scenario prose states the observed behavior, never a guess.
- **Imperfect first attempts are fine when flagged.** Say what needs
  follow-up and which questions are open.
- **The user merges within minutes** and asks for the next thing; sync main
  before new work. They test after merging (not production code yet), so
  bugs they find arrive with the next request.
- **Scope grows in flight** — the user expects you to adapt. Keep each PR
  internally consistent and say when a follow-up would be cleaner as its own
  PR.

---

## 7. Subsystem reference

How each part of the engine works NOW, with the invariants that bit before.
Rule details live in RULES_REGISTRY descriptions; scenario ranges point at
working examples. Standing direction from the user: **more gamerules, fewer
hardcodes**.

### 7.1 Units, vars and stores

- **Units** (`Entity`): id (immutable — `!ent rename` changes only the display
  name), name, x / y (1-based), facing, vars, status, passives, clamps, plus
  the protected link fields `part_of` / `mounted_on` / `mount_slot`. Vital
  vars hp / max_hp / initiative (names from `hp_var` / `max_hp_var` /
  `turnorder_var`).
- **`Entity.write_var` / `remove_var`** are the only way to change vars:
  vital vars are coerced to finite whole numbers or refused, nothing nests
  under a vital, vitals can't be deleted (`allow_protected` only for the
  property setters), reserved paths (x / y / name) are refused, a footprint
  write must fit (`check_body_fits`), then clamps, var hooks and the death
  check run. Wholesale var sets (spawn, summon / part / segment templates,
  transform) run `checked_unit_vars` instead.
- **Every store** (unit vars, team data, match vars, tiles, zones, statuses)
  goes through `_own_value` (deep copy, string dict keys, JSON values only),
  `check_store_path` (no empty path segment) and `check_no_value_ancestor`
  (no `k.x` while k = 5). Status number fields (level, duration) via
  `checked_status_value`.
- **`default_entity_vars`** (`!defvar`) fills MISSING vars at spawn, before
  vital validation; `!ent add` / template values win.
- **Clamps** (entity + system `default_clamps`, `!clamp` / `!gclamp`): hard
  always clamps; soft engages only when a write crosses from the legal side
  and stays dormant past the bound; max applied before min. The default soft
  clamp caps hp at max_hp.
- **Aliveness:** `alive_condition` rule (empty = hp > 0 or indestructible);
  the death PIPELINE uses `death_condition` (+ per-unit `__death_condition`,
  `death_condition_mode`).
- **Commands:** `!ent` add / remove / rename / info / dump / diff / set_var /
  set_vars / delete_var / hp / init / tp / move / push / pull / swap / face /
  kill / revive / clone / copy / transfer / transform / revert / status /
  group / action / store_entity_into_var. `!ent hp` SETS by default (`5`, `=-3`, `max`, `$(...)`);
  a leading sign changes (`+5` heals, `-5` damages); one `write_var`, hook
  output and deaths reported (633-634, 647). `!ent set_vars <id> k=v ...` is
  one all-or-nothing undo step (684). `!ent set_var` coerces only lowercase
  `true` / `false` to bools and keeps `inf` / `nan` as text.
- **Container helpers** (the engine has no inventory concept): `var_add`,
  `var_move` (looting), `var_copy`, `var_sum_field`, `item_add` /
  `item_consume` (drops the container at 0; `amount_field` rule), `var_keys`,
  `var_sum`, `var_clear`, `get`, `keys` / `values` / `index_of` / `unique`
  (547-548, 685-686). Authoring notes: in an item's action `source.<path>` is
  the ITEM's container and `entity[self]` the HOLDER; dict keys are unique, so
  identical items stack through an amount field; `!ent set_var hero
  inventory.potion 99` replaces the whole item dict.
- **`!ent clone`** copies the whole part subtree, strips mount links, refuses
  a body part, and is all-or-nothing (`_rollback_match` on any refusal).
- **Summons** (`Match.summon_entity`): `summon(template, x, y)` /
  `summon_near(..., radius)` / `summon_from(path, ...)` turn an entity-shaped
  dict into a unit (id minted from its name, footprint placement, parts /
  segments keys, on_entity_spawned), bounded per command by
  `summon_event_limit`; `entity_snapshot(eid)` and `!ent
  store_entity_into_var` make such a dict; `remove_entity` despawns. Server
  templates (§7.17) build on the same path.
- **Occupancy:** one unit per cell, except units with the `__cell_stackable`
  var; `cell_entity` names the blocker, `entities_at` everyone covering a cell.
- **Shields / temp hp:** named absorb pools under the roots in
  `temp_hp_sources` (default `shields`; per-unit `__temp_hp_sources`), each
  `{amount, priority, tags, not_tags}` or a number. `absorb_damage(eid, n,
  tags)` drains matching pools highest priority first and returns what
  penetrates (the GM applies it to hp); `shield_total`. Decay is GM-composed
  (440).

### 7.2 Passives, hooks, events, watchers

- **Passives** (`Passive`, `!passive` per unit, `!gpassive` global, `!team
  passive` per team, `default_entity_passives`): `when` is a HOOK_NAMES entry
  or `event:<name>`. `Match._firing_passives(target)` yields global + the
  target's team passives; then the unit's own. Before each own-handler loop
  the unit's existence is re-checked (ghost firing, §5).
- **Var hooks** (`on_var_*`, `on_var_written`, `on_var_write_attempt`): run
  bottom-up for subtree writes; `self` is the CURRENT-TURN unit, and the
  extras carry changed_key / old_value / new_value / intended_value /
  was_clamped. Status hooks bind `self` = the affected unit. Recursion capped
  by `var_hook_recursion_limit`; `var_hook_warning_verbosity` (off / minimal /
  detailed).
- **Turn / round hooks** fire for the acting unit; its attached parts fire
  only their OWN passives (`own_only_targets`). Movement hooks per footprint
  cell (`fire_footprint_tile_*` / `fire_footprint_zone_*`) plus
  `on_entity_step` per step. Lifecycle: on_entity_spawned,
  on_entity_despawned (every non-death removal, fired BEFORE the unit
  leaves; `Entity.remove(despawn=)` returns its log), on_death / on_revive,
  on_entity_moved, on_mounted / on_dismounted, on_turn_skipped (`skip_status`), on_status_*,
  on_action_used / on_action_failed / on_action_used_on_target.
- **Event bus:** `emit(name, payload, target)` / `!emit`. Global handlers
  fire ONCE (self = target, else the current unit); a directed event also
  fires the target's team + own handlers. Payload via `event_get` /
  `event_has`; `event_name` binding; `event_recursion_limit`.
  `declare_winner` emits `match_outcome` (winner / reason / round) — the bus
  was chosen because `fire_hook` runs globals once PER TARGET unit
  (437-439, 568).
- **Watchers** (`!watch`, `Match.watchers`): an expression condition +
  program effect, EDGE-triggered (false → true), polled once after each
  top-level command settles; `once` removes after firing; `last`
  serialized; a malformed condition reads as not met (427-428).
- **Output:** every formula-driven change hands its log to
  `Match.surface_log` (action buffer, else the command's `FORMULA_LOG_SINK`),
  shown at the end of the command in order (651).
- **Schedules:** `schedule(delay, ...)` (round-based; raises under ATB),
  `schedule_on(eid, ...)` (turn-based), `cancel_schedule`; a removed unit's
  schedules go with it.

### 7.3 Turns, rounds and ATB

- **Turn order:** sorted by `turnorder_var` per the `turnorder_*` rules
  (direction, tiebreaker incl. `random_stable`, team grouping, change policy
  immediate / deferred). Before the first `!turn next` the pointer sits at
  the top of the order (a pre-start `!turn set` is lost if units are added
  after it — set the opener last, user call).
- **`!turn next`** (`Match.next_turn`): turn-end hooks / ticks → advance (round
  wrap fires round hooks, ticks, `round_start_message_format` line) → skip
  units whose statuses carry `skips_turn` (each skip fires on_turn_skipped;
  `_skip_to_eligible` tracks the ids it has seen, so removals mid-pass can't
  over- or under-run it) → `_start_current_turn`. The reply prints hook
  output first and "It is now X's turn" last; an unseen unit reads as "an
  unseen unit's turn".
- **Removal and death mid-turn:** removing the current unit sets
  `turn_vacated`, so the next `!turn next` starts the successor without
  skipping it; a unit that dies to its own turn-start effects passes the turn
  on at once (bounded by the order size). Every path that can empty the
  order returns cleanly.
- **Attached parts ride the parent's clock** (`_attached_tick_parts`): their
  statuses tick, turn/round own-passives fire and turn schedules run on the
  parent's turn; a part with its own initiative has its own turn instead.
- **Status ticks** (`fire_status_tick`): per status, the definition's `tick`
  at its `tick_when`, else the global `status_tick_formula` at
  `status_tick_when` (default never); stops for a unit removed mid-tick.
- **ATB** (`atb_enabled`, system-wide): no rounds; each turn goes to the unit
  whose charge bar (`atb_charge_var`, rate `atb_charge_formula`) fills
  soonest (ties: higher rate, then id; rate ≤ 0 can't act); reset via
  `atb_reset_formula` (default subtract `atb_threshold`). A skipped unit's
  turn still elapses (ticks fire, action surface doesn't). `round_number()`,
  `turn_index()` and `schedule()` RAISE under ATB; a one-time warning lists
  dormant round logic. `transform` resets the bar to the new form's value.
  Prims `atb_threshold()` / `atb_rate(eid)` (524-527).
- **`Match.turns_elapsed`**: serialized count of turn boundaries in both
  models (`_begin_turn`); `turns_elapsed()`; the clock for ATB-safe expiry
  (reveals record which clock they used) (566-567).
- **Pause:** `!match pause [reason]` refuses players' state-changing
  commands; `pause_affects_hosts` holds hosts' commands until `!match
  resume` runs them (`resume drop` discards); never an undo step (637-638).

### 7.4 Statuses

- **Instances** live in `entity.status[name]` (level, duration, any data);
  raw editing via `!ent status`. **Definitions** (`Match.status_definitions`,
  `!status def/tick/when/stack/maxlevel/data/tags/removes/blockedby/sprite/
  drop/list/info`) describe a status ONCE; an instance resolves behaviour from
  the definition of the same name. Duration decrement and self-removal live
  in the GM's tick formula; the engine forces no decay (390-391).
- **Applying** (`apply_status` / `status_apply` / `!status apply`): stacking
  from the definition's `stack`, else `status_default_stack` (refresh /
  add_level / extend / replace / none). `max_level` is a hard ceiling
  everywhere (`_cap_status_level`).
- **Prims:** `status_apply` / `status_force` / `status_get` / `status_set` /
  `status_has` / `status_tags` / `status_has_tag` / `statuses_with_tag` /
  `status_resist_of` / `is_status_immune` / counters / dispel / transfer.
- **Tags and tokens:** a definition's `tags`; a token is a bare name or
  `tag:<x>` (`_status_token_matches`), used by `removes` / `blocked_by`
  (checked before stacking), immunity, resistance and dispel.
- **Resistance / immunity** are source-gated like modifiers:
  `status_resist_sources` (default `equipped`, per-unit replace / add vars)
  plus innate `status_immune` / `status_resist` vars; `status_resist_stack`
  (sum / max / first). Resistance reduces the applied LEVEL only, and only
  when a level is set (first application, add_level, replace with a level —
  `_resistance_applies`); ≤ 0 = fully resisted. `status_force` / `!status
  force` ignores resistance and immunity (blocked_by and the part rules
  still apply). `status_apply_block_reason` explains a refusal (449-452,
  496-497).
- **Body parts:** `part_status_immune` / `part_status_redirect` rules (per-part
  `__status_immune` / `__status_redirect`) run first in apply_status.
- **Counters:** `status_counter_add/set` (`!status counter`) on any numeric
  field; the status is removed at ≤ 0 (durations and charges alike). A
  counter on a text field is refused.
- **Dispel / transfer:** `status_dispel(eid, token, max)` (token-only, no
  undispellable guard, user call); `status_transfer(from, to, name)` removes
  from the source unconditionally and re-applies on the destination through
  apply_status — resistible, consumed on rejection, and the moved instance
  keeps its custom data (`seed_data`) (528-529, 683).
- **Display:** status sprite overlays (§7.13).

### 7.5 Modifiers (derived stats)

- Base stats stay plain vars; a modifier is a data record `{stat, op, value,
  tags, not_tags, priority, condition, grants_tags}` aggregated live by
  `_raw_modifier_records` from status instances, a unit's `modifiers` var,
  scanned roots (`modifier_sources`, default `equipped`; per-unit
  `__modifier_sources` / `__modifier_sources_add`) and team data (`team:<t>`).
  A bundle is a list or a dict of named records.
- `value` / `condition` may be formulas with `self` = owner plus the call's
  `target` / `attacker` / `defender` / `other`.
- **Fold** (`apply_modifiers`): effective priority = priority + per-op offset
  (`modifier_op_priority`), same-op combine within a tier (add sum, inc% sum,
  more% product, set last, min floor, max cap), tiers ascending,
  `modifier_op_order` breaks ties; `modifier_stat_caps` clamps the result.
  An unknown op folds as add and `!mod show` flags it.
- Pure query: the engine never applies modifiers; the GM threads
  `apply_mods(...)` / `list_mods(...)` through their own formulas. `!mod show
  <eid> <stat> [base] [tag ...]` (399-400, 406-408, 480).

### 7.6 Actions and choices

- **Actions** are data at `vars.<container>.actions.<name>` (`target` type:
  entity / location / entity_list / location_list / none / corpse /
  corpse_list; `body` program). `discover_actions` walks containers
  (`action_container_mode` / `_paths`); mount slots add their own (§7.10).
  `!action <eid> <name> [target] [k=v ...] [answer=...]`, `use_action`,
  `has_action`.
- **`run_action`** is transactional: pre-state captured, body run,
  `fail()` / an exception rolls the match back in place
  (`_rollback_match`: serialized state restored, runtime fields and undo
  history preserved). A failing NESTED action rolls back only its own
  effects (check `use_action`'s result to abort the outer one).
  `action_recursion_limit`. `cmd('...')` dispatches commands on
  `action_cmd_allowlist` (default `ent`) through `_BufferCtx`; action bodies
  can't run admin commands or save templates (no admin flag, no identity).
- **Choices:** `choose(prompt, options)` / `choose_number(prompt, lo, hi)`
  replay the body: on an unanswered choice the attempt rolls back and re-runs
  with answers replayed in order (`answer=` tokens, else the surface's
  `prompt_choice`; the harness must pre-supply). Per attempt the RNG, output
  buffer and summon budget are reset, so side effects before a choice apply
  once. `cancel` aborts; `action_choice_limit` (394-395).
- **Rider actions:** a rider gets its slot's actions plus the vehicle's
  actions whose `allowed_slots` list its slot; `mount_action_actor` decides
  who is `source`; `vehicle` / `rider` / `slot` are always bound.
- **Composed patterns** (no engine feature needed): channeled actions break on
  move / damage through passives (442); charge-up attacks count turns in a
  var (443); armor-limited penetration loops `entities_on_los` with an
  accumulator (393).

### 7.7 Command scripting

- **Inline `$()` args:** any command arg `$(...)` is evaluated as a READ-ONLY
  expression after the access gate (`raw_args=True` commands — eval, batch,
  run, foreach, macro, ent — resolve their own). `validate_arg_safe` allows
  only `ARG_SAFE_FUNC_NAMES` (no mutators, no `!func`s); a result with spaces
  stays one argument. `inline_args_access` = host refuses players' `$()`
  (fog matches: formulas see everything) (539-543, 549-550).
- **`!batch a ; b ; ...`** = one undo step; `!batch strict` stops at the first
  ❌ and undoes the lines before it (every match restored in place, created
  matches removed, deleted ones restored, pointers and rules restored;
  bot-wide settings are not) (692-693). **`!run <file>`** reads a saves-folder
  file (admin). Both nest within `macro_recursion_limit`.
- **Macros** (`!macro set/run/list/show/remove`): `$1..$N`, `$@`, `$#`
  (single-pass substitution); control flow `if / elif / else / repeat / end`
  with read-only conditions; `macro_repeat_limit`, `macro_step_limit` (counts
  dispatches AND loop iterations), `macro_recursion_limit`; one undo step
  (425-426, 544-546, 555-556).
- **`!foreach <selector> ; <cmd> [; <cmd> ...]`**: the `!find` selector
  grammar; `$id $name $x $y $team $i $n` substituted single-pass; targets
  resolved before any command runs; one undo step. Host-gated, EXCEPT that a
  sweep whose every inner command is player-available downgrades to `all`
  (`_foreach_read_only`, default-deny: aliases, `$` in the command position,
  self-dispatching commands and elevated args keep it gated). A non-host's
  sweep skips units their POV can't see (486-490, 557-558, 564-565).
- **`!find`** selectors: `var<op>value`, `status:`, `group:`, `action:`,
  `near:<eid>:<r>`, `within:<x>:<y>:<r>`, a leading `!` negates, `|`
  alternatives (an alternative that doesn't parse reuses the previous kind and
  key); `show:<csv>` (incl. `status`), `sort:<var>[:desc]`, `count`, `ids`
  (536-538, 631, 694-695).
- **`!assert [--as eid] "<expr>" [msg]`**: read-only; a failure raises
  `AssertionStop`, and batch / run / foreach / macro stop on the `ASSERT_STOP`
  sentinel; in an action, `cmd('assert ...')` fails the action (601-603).
- **`!again`** reruns the caller's last top-level command in the channel
  (runtime-only `mgr._last_commands`; never records `again` / `as` / clicks;
  refused inside dispatch_no_snapshot; always player-available) (639, 646).
- **Aliases** (`!alias`, per match; system aliases via `!system alias`)
  resolve BEFORE the access gate. `!func` defines formula functions
  (`formula_function_recursion_limit`). `!eval [--as eid]` runs a formula.

### 7.8 Geometry, movement and blocking

- **Grid:** square cells, 1-based; `max_grid_dimension` (default 500, owner
  ceiling) checked on create and resize. `!map resize <w> <h> [anchor]`
  shifts everything coordinate-bearing by the anchor's offset (units, tiles,
  corpses, zones, fog memory, camera views, snake trail vars); a shrink that
  pushes units off follows `map_resize_shrink_mode` (block / kill, looping
  until nothing is off-grid).
- **Movement verbs:** `!ent tp` / `move` (count first, `allow_diagonal_movement`)
  / `push` / `pull` / `swap`, group moves (`group:<name>`,
  `mount_group_move_mode`), formula `move_entity` / `move_step` (a real
  one-step walk) / push / pull / swap. Every verb validates the WHOLE swept
  footprint and the final footprint (`_validate_placement`,
  `_occupancy_ignore` for the mover's own body / segments / group), fires
  per-cell hooks and on_entity_step, stops when the mover dies, and
  redirects a mounted driver to its vehicle (`_mount_move_redirect`; a
  passenger is refused). The raw `move_to` and spawns are never gated.
- **Blocking:** a cell blocks a mover when its tile or a covering zone or a
  corpse evaluates truthy: tile `block` data > template > `tile_block_condition`;
  zone `block` data > `zone_block_condition`; `corpse_block_condition`
  (bindings corpse_id / corpse_team). Expressions with `self` = mover, or bare
  bools; FAIL-OPEN (a broken or unreadable condition doesn't block — give
  gating vars a `!defvar` default). `block_walk/tp/push/swap` rules: walk /
  tp / swap raise `Blocked`, push / pull stop before the wall. `!map cell
  <x> <y> for=<eid>` explains a cell, naming conditions that failed.
  `cell_blocks` (raw), `_check_block(mode)`, prim `cell_blocked`.
- **Facing:** `!ent face <id> <dir|cw|ccw|toward ...>`, `set_facing`,
  `face_toward` all via `set_entity_facing` (`allow_diagonal_facing`;
  re-seats region-slot riders). `facing_toward` is angle-based from the body
  centre; `direction_to` snaps by the signs of dx / dy — (5, 1) is `right`
  for one and `down_right` for the other (613-614). New units face per
  `spawn_face_toward_center` / `spawn_default_facing`; template spawns and
  restored parts keep their saved facing.
- **Teams and relations:** a unit's team is the `team_var` var. `relation`
  arguments filter any / hostile / ally / same_team / attackable
  (`is_hostile`; `is_attackable` honours `friendlyfire`).
- **Directional sides:** `relative_angle`, `relative_side`, `side_hit(target,
  x, y[, sides, corner_arc])` (`front` / `back` / `left_side` /
  `right_side`, plus diagonal corners when `sides=8`, each corner spanning
  `directional_corner_arc`°), `directional_get(eid, base, x, y)`,
  `facing_of`. Multi-tile targets use the box hitbox (`side_hit_hitbox_mode`):
  the bearing is scaled by the footprint half-extents (434-436).
- **Flanking:** `flanking_angle` (bearings from the target's centre at least
  `flanking_min_angle` apart, box-scaled), `flanking_line` (the segment
  between the two flankers crosses opposite sides / corners of the target's
  body, exact arithmetic), `flanking` per `flanking_mode` (687-688).
- **Distance and areas:** distance to a body = NEAREST footprint cell
  (`entity_gap_distance`, `cell_entity_distance`, `_rect_gap`); metrics
  square_radius (Chebyshev, default) / manhattan / euclidean — an unknown
  metric is refused. `entities_within`, `nearest_entity` (relation SECOND),
  `entities_in_area/cone/rect/zone`, `cells_in_*`, `chain_targets` (relation
  judged against the ORIGIN), `entity_distance`, `nearest_cell`,
  `free_cell_near`, `random_cell`, `random_free_cell(..., fit=body|center|any|
  anchor)`, `entities_at`, `cell_entity`, `grid_width/height`, `in_bounds`.
  Membership in an area = ANY covered cell. Builders are clipped to the map
  and charged to `formula_cell_limit`. `!dist` and `!map preview
  <burst|cone|line|rect>` show the same geometry.

### 7.9 Multi-tile bodies

- A unit covers a W×H rectangle anchored at its TOP-LEFT cell; W / H are vars
  named by `footprint_width_var` / `footprint_height_var` (absent or < 1 =
  1). A write that grows the body must fit (`check_body_fits`: newly covered
  cells on-grid and free); a side reads as at most grid side + 1
  (`_cap_footprint`).
- Core: `entity_footprint`, `entity_cells` (row-major, [0] = anchor; region
  parts return their region), `entity_occupies`, `cell_occupant`. Policies
  (user calls, hardcoded): distance = nearest cell; membership (fog, AoE,
  zones, lines) = any cell; outward vision = union of every cell's sight.
- Movement hooks fire per covered cell (a 2×2 crossing a fire band burns once
  per fire cell). A large corpse keeps one id; its cells derive from its
  stored footprint vars. Prims: `footprint_width/height`, `footprint_cells`,
  `occupies`, `entity_center` (floored), `aoe_origin` (`aoe_origin_mode`)
  (382-386).

### 7.10 Body parts, segments and mounts

- **Parts** are real units with the protected `part_of` field; a parent's
  parts are derived by scanning (`parts`, `part`, `has_part`, `part_of`,
  `entity_part_subtree` BFS parents-first). `parent` token. Created by
  templates (`parts` key, `{role: part}` or a list) or `!part
  add/attach/detach/remove/glue/locate/region/segment/list/info`.
- **Kinds:** GLUED (mirrors the parent's anchor, skipped by occupancy, render,
  vision, enumerators, roster unless `roster_glued_parts` / `__roster_show`),
  LOCATED (`__part_located`: own cell, renders, occupies, targetable),
  REGION (`__part_region`: a facing-aware region of the parent's footprint —
  front / back / left / right / center / all / corners; drawn over the parent
  only with a custom glyph, `part_custom_glyph_priority`). 0/0 parts are
  indestructible pass-throughs (`is_indestructible`).
- **Damage:** only `damage_part(part, n)` routes to main: `to_main_percent`
  (default `part_to_main_percent_default`), `to_main_cap` (none / max_hp /
  remaining_hp / absolute:<n ≥ 0> — a typo is refused), `vital`,
  `indestructible`; the transfer is computed from pre-hit values and applied
  to the parent first; `rounding_mode`. A destroyed part lingers dead, fires
  on_death once (`__part_destroyed` latch, cleared by any heal above 0) and
  kills the parent if vital. Negative amounts are refused.
  `damage_spread(target, total[, mode, fragments, origin, radius])` divides
  a total across parts (`aoe_default_mode`: weighted by `aoe_weight` /
  hit_weights, uniform, fragment × `aoe_fragment_count`, main_only;
  largest-remainder) and skips parts a cascade removed. Property searches
  (`entities_with_status` / `_var`) include parts; spatial ones skip glued
  parts. `hit_location(target,
  x, y[, aim, aim_weight, aim_bonus, mode])`: TWO modes, weighted
  (`hit_weights.<side>`) and uniform; aiming is the separate `aim` arg (that
  part's weight × aim_weight + aim_bonus) (396-417, 467).
- **Lifecycle:** parent death snapshots the whole subtree into the corpse and
  revive restores it (displaced located parts go to the nearest free cell
  with ⚠️, dropped only when nothing fits). Removing a unit removes its
  subtree. Killing a part is limb destruction, never a corpse. Detach makes a
  free unit. Parts share the parent's turn clock (§7.3).
- **Snake segments:** located parts with `__segment` + `__follows` (the one
  ahead); `snake_segments(head)`. Follow `segment_follow_mode` (trail /
  path + `segment_spacing`; head override `__segment_follow`); a teleport
  re-lays the body (`_resettle_snake`); `segment_self_collision`.
  `segment_death_mode` (none default / solid / cascade / split, with
  `segment_split_head_template`) when a segment is destroyed;
  `segment_removal_mode` (close default / cascade / split / death) when a GM
  despawns one (`despawn_entity`); internal removals close the chain
  (`_splice_out_segment`). A promoted head never acts by itself (418-422,
  592).
- **Mounts:** a vehicle is any unit with a `slots` var; a slot has
  `capacity`, `cost` (formula), `condition` (formula, fail-open), `region`
  (where the rider is drawn and targeted; none = hidden inside),
  `controls_movement`, `actions`. Links are the protected `mounted_on` /
  `mount_slot` fields; occupancy derived (`vehicle_riders`, `slot_occupants`).
  Moving the vehicle carries riders, nested (`_restamp_riders_for`); a
  driver's own move moves the rig. Hidden riders are off the ground,
  unrendered, unseen and grant no vision (`hidden_rider_grants_vision`).
  `mount_on_host_death` (eject / kill / keep); a dead rider revives
  unmounted; transform re-checks a mounted rider's slot (and a vehicle's
  riders) per `transform_rider_mismatch_mode`. Boarding legality is the GM's
  call through slot conditions — a built-in boarding range was REJECTED.
  `!mount <rider> <vehicle> <slot> | dismount | switch | list | info`; prims
  mount / dismount / switch_slot / riders / slot_riders / can_mount / ...
  (458-461, 465-466, 503-506, 682).

### 7.11 Death, corpses, revive, transform, disguise

- **Death** (`_process_death`): `death_result` (corpse / delete, per-unit
  `__death_result`); a corpse is the unit's snapshot in its cell's tile data
  (`corpses.<id>`, mount links stripped); `corpse_id_uniqueness`. `kill` /
  `revive` run `default_kill_function_effects` /
  `default_revive_function_effects`. Prims: has_corpse, corpse_at,
  all_corpses, corpse_var / corpse_has, corpse_status_has / _get / _names;
  corpse / corpse_list action targets.
- **Transform** (`!ent transform <id> <ref> [stash]`, `transform` /
  `revert`): swaps name, vars, passives, clamps, statuses and the part
  subtree; keeps id, position, facing, team, turn slot and the unit's own
  segment linkage; hp per `transform_hp_mode` (percent / keep / full); the
  old statblock is stashed at a caller-chosen var path, so transforms stack.
  New vars are checked before the old parts go (481-483, 586).
- **Disguise** (`disguise_var`): `{name, glyph, glyphs, color, vars}` shown
  to viewers outside the unit's team (maps, roster, `!ent info`, `!find`
  rows); mechanics always read the real unit; overlays hidden under a
  disguise (484-485).
- **Outcome:** `declare_winner` / `!match win`, `match_winner()`,
  `match_over()`; win conditions are composed (watchers, on_death passives),
  never configured.

### 7.12 Zones, tiles and auras

- **Tiles:** per-cell data (`!tile set/place/line/fill/copy/...`), templates
  (`!tile def`, per system), precedence instance > template > rule for
  glyph / color / sprite / block / opaque; tile hooks (on_enter / on_exit /
  on_stop / time hooks).
- **Zones:** named cell sets (`!zone ...`): data, hooks (boundary + per-cell
  + time), dedicated glyph / color / sprite fields, `zone_shift`, `zone_*`
  prims, `zone_distance`.
- **Auras:** a zone anchored to a unit (`!zone anchor <name> <eid> [radius]
  [metric]`): cells re-stamped around every footprint cell when the anchor
  moves, without firing the aura's own enter/exit. On anchor loss
  `anchored_zone_on_anchor_loss`: delete / freeze / suspend (cells cleared,
  binding kept, resumes on revive or a part heal). The binding is by id, so
  a suspended aura also resumes around a LATER unit that reuses the id
  (left as-is) (388-389, 519-520).

### 7.13 Vision, fog and line of sight

- **Visibility conditions** (expressions; empty = visible; a broken one shows
  everything): `entity_visibility_condition` (`self`, `pov_team`),
  `tile_visibility_condition` (`tile_x` / `tile_y`), `zone_visibility_condition`
  (`zone_name`), `corpse_visibility_condition` (`corpse_team`, no `self`).
  All through `Match._visibility_visible`; `entity_visible_to` etc. Your own
  team's units (root body's team) are always visible to you while
  `pov_own_team_visible` is on (`own_team_unit`).
- **Fog** (`Match.fog_enabled`, per match, `!match fog on|off`): a team sees
  the union of cells within each alive member's `fog_vision_radius` var
  (metric `fog_range_mode`), from every footprint cell. With `fog_los` the
  fog also needs a clear line. Memory (`!match fog memory on|off`,
  `Match.explored`, `fog_memory_mode` full / terrain). `!reveal_fog <team>
  ...` adds reveals, optionally temporary (`turns=N`, keyed to whichever
  clock advances). Everything funnels through `_fog_team_sees`; renders
  memoize it for one pass (`_vision_memo`).
- **Opacity / LOS:** `tile_opaque_condition` / `zone_opaque_condition` (`self`
  = viewer), per-cell `opaque` data; FAIL-TRANSPARENT. `has_los`,
  `raycast` (where a beam lands), `first_opaque` all walk ONE corner-aware
  path (`_los_stop`; `los_corner_mode` permissive / strict / open), symmetric,
  clipped to the map; the viewer's cell and the target's own opacity never
  block — a unit standing ON the first opaque cell is seen / hit (user call:
  intended). `entities_on_los` (strictly between, sight-aware),
  `entities_in_line_ignorelos`, `entities_in_line_until` (cap). Entities never
  block sight in fog; the GM composes body blocking in actions.
- **Sight prims:** `can_see`, `team_sees_cell`, `team_sees_entity` (range AND
  los; `_rangeonly` / `_losonly` variants; all ignore the fog toggles),
  `visible_entities(eid[, relation])` (minus units the stealth rule hides
  from the viewer's team), `!map ent_sight <eid>` (cells one unit sees; a
  player may ask about their own team's units). Costs charged to
  `formula_cell_limit` / `sight_check_limit` (690-691, 701-703).

### 7.14 Access, approvals and POV

- **Identity:** `ctx_user` / `ctx_user_name`; the CLI and harness switch it
  with `!as host|player <name>|owner`. Admin (`ctx_is_admin`: Discord
  Administrator; CLI / GUI always) and bot owner (`ctx_is_bot_owner`) are
  separate tiers (§3).
- **The gate** (`_gate_decision` / `_effective_access`): per command
  `access=` all / host (default; a player's command is QUEUED for approval) /
  host_only / owner. Downgrades: `READ_ONLY_SUBCOMMANDS[root]` (per root),
  `READ_ONLY_BARE_ROOTS`, the read-only `!foreach`. Upgrades: `ELEVATED_ARGS`
  (`full`, `resize`, settings...) except the read forms in
  `_ELEVATED_READ_FORMS`. Overrides: `Match.access_overrides` (`!host access`,
  per match, kept out of `rules` so a refresh can't wipe it) beat the
  `command_access` rule (`!system access`, per system). No-op without an
  active match, identity or owner — hence the separate admin and
  target-host checks.
- **Approval queue:** `Match.add_pending_request` (runtime-only; carried
  across undo); `!pending` / `!approve` / `!deny` / `!cancel`, Discord
  buttons. Approved commands run with the approver's authority in the
  requester's channel against the request's match (`run_approved_request`,
  `_channel_pointed_at`).
- **Hosts:** `Match.owner` + `cohosts` (`!host add/remove/list`, owner only).
  Pause (§7.3).
- **POV:** a channel's view is its binding's `pov` (`!match bind pov=<team>`;
  absent = omniscient), or a CLI `!as view` preview, or a host's one-off
  `as=<team>` (`_view_pov`). `full` forces omniscient (host).
  `pov_filters_queries` (default on): under a team view `_query_eid` makes a
  hidden unit read exactly like a missing one and listings skip it
  (`_pov_hides`) across `!ent info`, `!dist`, `!find`, `!foreach`, `!turn`,
  `!map center/cell`, `!part`, `!mount`, `!action`, `!passive`, `!clamp`,
  `!mod`, `!schedule`, `!tile info` (corpses inside tile data too);
  whole-board reads (`!history diff`, `!log`, `!history list <kind>`, undo
  preview) are host-only while anything is hidden
  (`_whole_board_read_blocked`). `team_data_visibility` (own / all / host)
  filters `!team list/get`. `inline_args_access` closes the `$()` route
  (579-580, 593).

### 7.15 Rendering

- **ASCII** (`render_ascii` → `_render_ascii_impl`): layers zones < tiles <
  units < fog; glyphs (`glyphs.<facing>` > `glyph` > facing arrow; one
  character), colours (`color` var > `team_colors` / a palette-named team;
  ANSI only when `ctx.supports_color`, one code per run), legend
  (`map_legend_by_default`, `!map legend`, `legend=`), coordinate rulers
  (`map_coords`, `!map coords`, stacked column digits), hidden layers (`!map
  layer`, `hide=`), preview / sight marks (`preview_glyph`), viewport.
- **Viewport** (`viewport_width/height` 28, `viewport_mode` auto = only
  surfaces with `viewport_capable`): per-channel camera `Match.channel_views`
  (`!map pan/center/view`). Discord fits a 2000-char message by dropping
  rulers, then colour, then legend.
- **Graphics:** `render_scene` (→ `_render_scene_impl`, parallel to the ASCII
  path over the same predicates) emits a declarative model; the engine never
  loads an image. Sprite keys mirror glyphs (`sprite` / `sprites.<facing>`,
  `sprite_mirror` fills missing facings, `fallback_sprite`), tile / zone /
  corpse sprites, background (`background_sprite` default `ground_default`,
  `background_mode`, placed on the whole grid), fog (`fog_sprite` default
  `fog_default`, `fog_opacity`), borders (`show_borders`, per-match `!map
  border`, per-tile overrides), `sprite_mode` (stretch default / single /
  tile), z-layers (`sprite_layer_*`, per-item `sprite_layer`), overlays
  (status definition / instance `sprite`, the `overlays` var), corpse look
  (`corpse_sprite(s)` > `corpse_default_sprite` > the living sprite greyed by
  `corpse_sprite_tint` / `_opacity`), team colour as an outline
  (`team_outline_width/opacity`, `team_tint_opacity`), colour-only cells
  (`tint_fill_opacity`), legend, rulers, highlights (`preview_color` /
  `preview_opacity`). `sprite_render.SceneRenderer` draws it; glyphs outside
  ASCII use a font that has them (`fonts/` folders, then system fonts).
- **Surfaces:** `!map image`, `!map mode text|image` (plain `!map` posts an
  image), `!map autoupdate` boards (Discord, runtime-only), `!map scene
  [list]` (textual model), gui.py (canvas; zoom / pan local to the GUI).
  Sprites come from `sprites/` and per-server `data/<server>/sprites/`
  (server-side only — there is NO in-chat upload, deliberately).
- **Intended ASCII / graphics differences:** corpses and overlays are
  graphics-only; a multi-tile body follows `sprite_mode`; colour-only cells
  are a translucent fill; a unit's colour is an outline around its sprite;
  the image legend lists every look in the window (ASCII only top-layer
  glyphs); layer order follows `sprite_layer_*`. Everything else must agree
  (§2 parity pass) (530-535, 711-717).

### 7.16 Matches, history and the table

- **Matches:** `!match new/use/list/info/bind/unbind/channels/rename/delete/
  clone/fog/pause/resume/var/win/outcome/hosts`. `!match clone <new_id>`
  copies everything but bindings, cameras, pending requests and history; the
  cloner owns the copy, co-hosts and access overrides carry, and when a
  co-host clones the original owner becomes a co-host. Ids follow `ID_RE`.
- **Systems and rules:** a `GameSystem` holds rule overrides, tile
  templates, functions, aliases, default vars / passives / clamps. A match
  copies its system's rules into `match.rules` at creation and on every
  `refresh_match_rules` (any `!system set` / `!system access`); per-match
  settings that must survive a refresh live in their own fields
  (`access_overrides`, `fog_enabled`, `fog_memory`, `border_*`,
  `render_mode`, `map_coords`, ...). Rules are left out of match files on
  disk and rebuilt from the system on load / restore.
- **Match vars:** `!match var`, `match_var_get` / `match_var_set` /
  `match_var_del`. **Groups:** `!ent group`, `group:<name>` selectors and
  group moves.
- **Roster text:** `entity_line_format`, `entity_info_format`,
  `corpse_line_format`, `body_part_entity_line_suffix`,
  `mount_entity_line_suffix`, `show_corpses_in_entity_list`; `{placeholder}`
  templates over vars. Event log: `!log`, `event_log_*` rules, `log()`.
- **Whole-server saves:** `!store save/load <name>` (admin, saves folder;
  load is all-or-nothing); `!history export/import` (a snapshot from another
  match restores only the board).
- **Undo** (`match_history.py`): autosaves per round (`autosave_round_retention`),
  turn (`autosave_turn_retention_rounds`, under ATB `_turns`), command
  (`autosave_command_retention_turns` / `_max`), manual (`!history save`).
  `!undo turn|round|command [N] [confirm|preview]`, `!undo to round X`,
  `!history list/diff/restore/delete/export/import`; big undos ask for
  `confirm` past the `undo_confirmation_*` thresholds. A restore re-copies
  rules from the system, drops command snapshots past it, carries pending
  requests / held commands / `paused`, and handles channel bindings per
  `undo_channel_bindings_mode` (keep / revert / confirm, `bindings=`
  override). The diff (`_format_snapshot_diff`) reports positions, vars,
  statuses, names, links, tiles, zones, groups, match vars, team data, the
  turn and "other changes". The seeded RNG position is not in snapshots
  (user: fine).
- **Team data:** `!team set/get/add/list/clear/passive`, `team_get/set/add`;
  a `modifiers` bundle applies to every member.
- **Dice and tables:** `roll("NdM±k")` with explode `!` and keep `kh<n>` /
  `kl<n>`; `!roll`, `!roll odds` (exact distributions); `roll_table`,
  `band`, named tables (`!table`, `table_roll`); `pick` / `shuffle`. All use
  `Match.formula_rng()` (honours `random_seed`, replay-safe).
- **Other:** `!help find <words>` (commands, subcommands and rules),
  `!whoami`, `!log` (event log rules), `!state`, `!list`, `!dist`, string
  helpers (`upper` ... `fmt`, a safe field parser), list helpers (`sum`,
  `sorted`, `any`, `all`, `count`), `lowest_var` / `highest_var`.

### 7.17 Entity templates

- **What:** a server library (`MatchManager.templates`,
  `data/<server>/templates/<name>.json`) of units saved without identity
  (`Match.capture_template`): name, vars incl. current hp, statuses,
  passives, clamps, facing, the part subtree. No id / x / y / links; part
  ids, `part_of` and `__follows` relative to `TEMPLATE_ROOT` (`@root_head`);
  part positions and snake trail vars as anchor offsets. `check_template`
  validates every write and load (vitals, reserved names, facing, status
  numbers, part links); a summon-style `{role: part}` dict is accepted.
- **Name = id prefix** (user call, "for now"): template `guardsman` spawns
  `guardsman`, `guardsman2`, ... with parts `guardsman2_head`.
- **Spawn** (`Match.spawn_template`): summon_entity, saved facing restored,
  parts through `_apply_statblock_parts` (which also keeps each part's
  stored facing); all-or-nothing.
- **Commands:** `!template save <unit> [name]` (host of the channel's match
  or admin; needs an identity), `spawn <name> <x> <y> [near=]`, `list`,
  `show <name> [path] [compact] [part=]`, `import <name> <json>` (create or
  replace — the text-editor route), `set` / `unset <name> <path> [part=]`
  (fields name / vars / status / passives / clamps / facing), `part remove`,
  `rename`, `copy`, `delete`, `trash [name]`, `restore <name> [n]`. Changing
  or deleting a template needs its saver (`saved_by`) or an admin; an
  admin's edit keeps the saver; a copy is the copier's. Reads are open to
  everyone (user call).
- **Storage:** written when saved, outside commit / rollback / undo, loaded
  at start even with match persistence off. Every replace or delete keeps the
  old version in `templates/.trash/` for 24 h, counted toward the server
  limit and cut first when room is needed (`write_template` makes room
  BEFORE moving anything). `!server wipe all` trashes templates.
- **Raw JSON tail:** `split_command_line` / `split_command_args` keep
  `template import <name> <rest>` whole (```json fence or single quotes
  dropped) on every surface; a multi-line import is one command on Discord
  and in the GUI.
- **Formulas:** `summon_template` / `summon_template_near` (mutating),
  `has_template` / `template_names` (read-only), via `CURRENT_WORKSPACE`
  (718-726).

### 7.18 Partial templates

Reusable var subtrees (an `axe_common` item, an `orc_base` statblock) in the
same library and namespace as entity templates: `{"kind": "partial",
"data": {...}}`; same permissions and version trash (727-731).
- **Two forms** (documented at `TEMPLATE_MARKER` in logic.py). FILE form, in
  templates: `{"__template": "axe_common", "damage": 8, "__removed":
  ["flavor"]}`: the partial's name, the changed values as plain keys (nested
  plain dicts merge key by key; anything else, lists included, replaces),
  deleted paths as tombstones. LIVE form, on units: the full data plus
  `"__template": {"name", "overrides", "removed"}`. A partial may itself
  reference one (`axe_common` extends `weapon_base`); an entity template's
  `vars` may (`vars.__template` = whole-unit inheritance), and so may its
  parts' vars. Refs must name partials; self-references and cycles are
  refused at save / import (`check_template_refs`); resolution charges
  `partial_template_depth_limit`.
- **Materialized, never linked:** spawn / summon / apply_statblock turn file
  refs into live data (`Match.template_materialize`; `resolve_partial`
  cached per command in `PARTIAL_CACHE`, keyed by server, match, pinning and
  name; any template command clears it). Library edits reach a unit only
  through `!template resync`.
- **Tracking** (`Match.track_template_change`, called by write_var /
  remove_var): the path goes into the NEAREST marker above it (a delete
  into `removed`); a value equal to the partial's at that moment takes it
  off ("back to the norm"), as does restoring a removed key; a listed parent
  leaves only when its whole subtree matches; engine writes (hp, ATB) count.
  The marker key can't be written or deleted by path (write_var refuses a
  `__template` segment; values carrying a well-formed live marker are fine,
  so looting with `var_move` / `var_copy` carries the marker along). Values
  bound for vars can't hold a FILE-form ref (`check_live_markers`).
- **Saving** (`capture_template` / `capture_partial` → `template_file_form`):
  each marked container becomes its name plus what deviates from the
  partial as this match sees it now; a listed dict that dropped base keys
  writes them as removed; a container whose partial is gone is written in
  full without the ref, with a ⚠️ note.
- **Commands:** `!template save <unit> [name] path=<var.path>` (`.` = all
  vars) saves a partial; `give <partial> <unit> <path>`; `show ... expanded`
  (resolved against the library, pins ignored, nothing pinned); `resync
  <unit> [path]` (all or nothing, tracking off while it writes; no path =
  the vars root or each outermost marked container); `detach <unit>
  [path]` (plain data); `refs <unit>` (player-available, POV-filtered).
  `delete` / `rename` of a referenced partial need `force` and an
  administrator: rename rewrites the refs in dependent templates (live
  markers keep the old name until resynced or saved; a rename that would
  close a cycle is refused before anything moves); delete writes the
  partial's own layer into each dependent (keeping a ref it holds itself),
  or the resolved data when the two layers don't combine. Formulas:
  `give_template(eid, path, name)` (mutating), `template_of(eid, path='')`
  (read-only); `var_keys` hides `__` keys, `!ent dump` shows them.
- **Pinning** (`template_pinning`, default off): `Match.template_pins`
  (`{name: {data, turn}}`, serialized, in undo snapshots, kept by `!match
  clone`; a transferred unit resolves against its new match) —
  `partial_data` pins a partial on its
  first use while the rule is on; off = pins kept, unused. `!match templates
  [refresh [name]]` lists them (with whether the library differs) or drops
  them; a save in a pinning match notes refs whose library copy differs.

---

## 8. Open questions, planned work, deferred and rejected ideas

Check here before proposing a feature: it may be planned, deferred or
rejected already. Update this section in the PR that resolves an item.

### Undecided (ask before changing)

- **Partial references in status instances and passives** (user: discuss
  after partial templates shipped): today only var subtrees reference
  partials; statuses and passives stay template-owned.
- **Status counter on a missing duration:** a status with no `duration` shows
  as ∞, but `status_counter_add(eid, name, -1)` reads it as 0 and removes the
  status. Whether ∞ should stay ∞ is open (user: decide later).
- **Approved commands and POV:** an approved player command runs in the
  requester's channel, so its output (including hook lines naming units that
  POV can't see) shows there. On hold at the user's request.
- **ATB bar across a transform:** transform replaces vars, so the charge bar
  resets to the new form's value. Raise it if the user wants it kept.
- **Aura id reuse:** a suspended aura resumes around a later unit that takes
  the old anchor's id (§7.12). Distinguishing them needs an identity concept
  beyond ids.
- **Path-mode snake transfer:** segments overlap early in `path` mode, so
  transferring a fresh snake can fail with "cell occupied".

### Deferred (the user wants these tracked)

- **Per-server CPU work budget** (revisit before any public rollout): the
  per-call limits multiply — 10,000 loop iterations × a 1,000-cell line on a
  500×500 map is still ~7 s of a frozen bot for every server. Preferred
  shape: a deterministic work counter charged per cell walked, loop
  iteration, macro step, summon and event, per server, owner-set. Needs
  careful decisions about compromises and abuse.
- **Undo history disk growth:** every snapshot is a full match state; if
  history becomes the bulk of a server's disk use, revisit retention defaults
  or a diff-based snapshot format.
- **Combat layers:** armor (coverage %, directional, damage-type AR vs ARP),
  the to-hit roll (accuracy / evasion / suppression, separate from
  hit_location), AP / FP / ARC action economy and REACTIONS (block / dodge /
  counter during another unit's turn — the choice-replay shape is the
  groundwork), fancier revive (regrow from a template). Interactive Discord
  menus for `choose()` (Discord relies on `answer=` tokens today).
- **Decide later (user):** configurable side NAMES for directional prims;
  per-damage-type `to_main_percent`.
- **Later:** nested maps per match (wanted); graphics mouse select / drag and
  animation ("foreseeable future"); a per-entity ATB threshold; arbitrary /
  L-shaped footprints and footprint rotation; per-rider footprints inside a
  vehicle, shared vehicle fuel / initiative, positional cover for riders;
  `trail` segment spacing > 1; more modifier context roles; elevation as a
  rule; vision caching beyond one render; a targeting-fooling "deep illusion"
  disguise; a force / reflect status transfer.
- **Not interested yet:** branching (tree-shaped) snake bodies.

### Rejected (don't propose again)

- Autonomous entity behavior / AI of any kind (§1).
- Hex or other non-square grids (§1).
- A built-in boarding range or "mount only from an adjacent cell" rule (slot
  conditions and the GM's actions decide).
- In-chat sprite uploads (while self-hosted; revisit only with validation,
  quotas and sandboxing).
- A `block_entities` flag on sight lines ("a tiny body shouldn't block a shot
  over it" — the GM composes it).
- An "undispellable" status guard (keep such effects outside the token).
- A built-in objectives table (win conditions are composed).
- Live-linked partial templates (materialize instead).
- An `on_facing_changed` hook; the minimap; struck idea-list items small #7,
  medium #156, large #45 (translation).

---

## 9. Final advice

Read this whole file before your first edit; the codebase has patterns, so
match them. When you finish a task, give a short, factual summary: what
shipped, the regression count, what you're unsure of. Keep this file a
reference of how things work NOW: describe new behavior in §7, move resolved
items out of §8, and put long narratives (audit passes, investigations) in
`docs/audit_history.md` or the PR description.

The user is a great collaborator — clear about goals, decisive on design,
blunt about mistakes. Build that trust by being precise and
process-disciplined. The system is fun to work on.
