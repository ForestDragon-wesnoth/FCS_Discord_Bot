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
- **What it flags.** `💥` (except the `💥 Unexpected error (ref N)` reply of
  `!as crash`, the error log's test hook) and "Syntax error" always; `❌ Runtime error:`,
  `❌ Unexpected error:`, an unknown subcommand ("isn't a valid command"),
  missing arguments ("is missing arguments"), a usage refusal (`❌ Usage:`)
  and stray words ("❌ Unexpected ... — usage:") unless the prose carries
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
- **Damaged files:** `?file corrupt <path>` (cuts the end off) / `?file
  delete <path>` change a file of the scenario's server folder behind the
  storage layer's back (it rescans its usage), then `!as restart` shows
  what a load does with it (864-866). `?file set <path> <a.b.c> <json>` /
  `?file replace <path> "<old>" "<new>"` write a file in the shape an
  older build left it (868, 874).
- **Tab completion** is tested in scenarios too: `?tab "<text>"` replies with
  what Tab does at the end of the text (`→ '<line>'` and `candidates: ...`)
  in the scenario's workspace; check it with `??` / `?!` (738-739).
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
  nonexistent helper name once hid in a function body). Its docstring is its
  `!help function` entry (`formula.function_reference`: first line the call
  form, then what it does; a wrapper in the namespace sets `__wrapped__`).
- **A HOOK_CONTEXT binding:** `_who_arg` handles HOOK_CONTEXT_NAMES by set
  membership, so `entity[<binding>]` works automatically; check the binding is
  actually supplied in the fire site's extras.
- **A gamerule:** schema keys — int rules carry `min` / `max` / `unlimited`
  (-1), str rules with fixed words carry `choices` (enum), formula-valued
  rules carry `"formula": "expression"|"program"`; a limit a server could
  raise high enough to stall the bot carries a ceiling (`"ceiling": True`,
  §3). Read it with the exact registry name (a typo'd `rules.get` returns the
  default forever). Schemas and defaults must be JSON (a system saves an
  overridden rule's schema; an import-time check refuses a `set`).
  Changing a rule mid-game must leave derived state consistent (switching
  `persist_undo_history` off once lost every snapshot already on disk). A
  rule that names an ENGINE var (hp, team, ...) goes in
  `logic.VAR_NAME_RULES` (§7.1; changing it renames that var on units); a
  rule that only SELECTS a var the GM keeps (`turnorder_tiebreak_var`)
  stays out, since changing it must rename nothing. A str rule whose value
  is a comma list of fixed words carries `"word_list"` (each word checked,
  `command_attribution`, `event_log_events`). A dict / list rule: decide whether a
  child system merges it entry by entry (`logic.MERGE_RULES`, §7.16) or
  replaces it whole; read a system's value with `mgr.system_value(s, key)`
  (it walks the inheritance chain), never from `s.settings`.
- **A serialized Match field:** `to_dict` / `from_dict` with deep copies on
  BOTH sides; a runtime-only field goes in `action._rollback_match`'s preserve
  list (`_check_rollback_fields` raises if a field is neither); decide whether
  undo restores it (`_NOT_UNDO_STATE`, `_restore_snapshot`) and whether
  `resize_grid` / `copy_entity` must shift or remap it.
- **A field or var that names another entity by id:** every path that copies
  or moves an entity across a boundary (corpse, clone, transfer/copy,
  transform, template save/spawn, revive) must remap or strip it (§5).
- **A command / subcommand:** an exact `annotate_sub` usage (the stray-word
  check and Tab completion read it; a NEW placeholder name that names
  something — a unit, a template, a rule — goes in `completion.py`'s slot
  tables, or it completes from the general name pool); alias spellings in `_SUB_ALIASES`; player-available reads
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
  that includes ids inside arguments (`near:<eid>` matched the visible units
  near a hidden one); re-run the POV leak detector and the existence-oracle
  detector (§5).
- **A read that prints formula text the GM wrote** (a body, hook,
  condition, passive, formula rule): print it through `logic.formula_text`
  (one text) or `_hide_formula_fields` (a dict / list view), so
  formula_text_visibility hides it (§7.14); re-run the formula-text leak
  probe (§5).
- **Anything iterating units and firing hooks:** re-check
  `eid in m.entities` after each hook (§5 "ghost firing").
- **A command whose effect on a match depends on more than that match**
  (its undo history, a file, another match, a runtime queue): add it to
  `_JOURNAL_STATE_ROOTS` / `_JOURNAL_STATE_SUBS`, so the command journal
  stores the state it left instead of a command a replay can't redo; a
  read of a server library journals don't carry goes through a recorded
  read instead (`logic.prefab_read`, §7.16).
  Anything a match state names (a generated name, an id) comes from the
  match RNG or a saved counter, never `uuid` / the clock, or every replay
  of it diverges (`sched_<n>` came from uuid). Re-run the replay
  differential (§5).
- **A change to server-wide state outside the systems** (a file, the
  template library, a deleted match): call `audit_note(...)` so `!server
  audit` names it.

### Usage strings are authoritative (stray-word check)

The dispatcher refuses positional words past the end of a command's usage
string (`_stray_words_error`): `!ent remove a b` used to remove only `a`. Usage
grammar: `<x>` one word, `[...]` optional, `|` alternatives (an alternative LED
by a literal word applies only when that word is typed), `...` / `<x ...>` any
number more, `key=<v>` / `[key=value ...]` options (left to the handler). Inside a
`<a ... | b ...>` group, an alternative's first word followed by `<...>` is
its literal action (Tab completion reads `when` in `!system status <... |
when <system> ...>` as the word, though `when` also names a slot). A
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

- **`logic.py`**: the domain model — `Entity`, `Map`, `Match`,
  `MatchManager`, `Workspaces`, `Passive`, `ClampSpec`, `SpecialTileTemplate`,
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
- **`journal.py`**: the per-match command journal (segments, entries, the
  state hash, the export bundle); recorded from `CommandRegistry._run`,
  replayed by `!match replay` (§7.16).
- **`prefab.py`**: prefabs (§7.19): `capture_prefab`, `check_prefab`, the
  `Turn` rotation / mirroring, `stamp_prefab` and its id / link remapping.
- **`completion.py`**: Tab completion for a typed line, shared by gui.py and
  cli.py (readline). It walks the typed words through the command's usage
  strings (`_usage_tails` / `_usage_tokens`, the stray-word check's
  tokenizer) and offers the literal words, `key=` options and names a
  placeholder takes (`_slot_names`: unit ids filtered by the channel's POV,
  templates, rules, var paths one segment at a time, ...). Read-only.
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
- **`docs/audit_history.md`**: the narratives of audit passes 2-44 — what was
  swept, verified clean, measured. Read the relevant passes before auditing
  an area again.
- **`docs/multi_map_design.md`**: the agreed design for multiple maps per
  match and nested maps (step 1 and 2a-2d built; §8).
- **`docs/zones_prefabs_design.md`**: the agreed design for zone
  definitions / tags and prefabs (#31; built, §7.12 and §7.19).
- **`docs/tags_design.md`**: the agreed design for widening tags (#260):
  one filter rule, unit / corpse / container / team / map / library tags,
  the vocabulary (built, §7.20).
- **`docs/feature_ideas.md`**: the open, numbered feature-idea list the user
  picks from by number (shipped / struck items listed at its top). Check it
  before proposing a feature. When a feature ships in full (everything its
  entry asks for or assumes), add its number to "Shipped" and DELETE its
  entry in the same PR (user directive); a partly shipped one keeps its
  entry, trimmed to what is still open.
- **`docs/combat_systems_fit.md`**: three tabletop combat systems the user's
  group wrote (Solis, FCS1, NCS), what the bot can't do for them and what
  costs needless work; the background of ideas #200-#255.

### The cross-cutting chokepoints (memorize these)

| Chokepoint | What flows through | What hooks in |
|---|---|---|
| `Entity.write_var` / `remove_var` | every var write / delete | store checks (vitals, reserved paths, footprint fit, partial-template markers), clamps, partial-template change tracking, var hooks (`on_var_*`), death check (top level only) |
| `Match._emit_status_diff` | every status change | status hooks (`on_status_*`), death check |
| `CommandRegistry.run` → `_run_top` / `_run` | every typed `!command` | command lock, alias resolution, access / admin gate, pause, inline `$()`, undo snapshot, formula log sink, `CURRENT_WORKSPACE`, watcher poll, storage commit, `!system undo` step + `!server audit` entry (`_record_server_changes`), command journal (`_journal_command`, `_journal_before` / `_after`) |
| `dispatch_no_snapshot` | batch / run / macro / foreach lines, action `cmd()` | admin check, inline `$()`, `!assert` stop sentinel, no gate (reached only from an already-gated command) |
| `action.run_action` | every action | pre-state for rollback, `_action_depth`, choice replay, on_action_used / failed / on_target |
| `Match.summon_entity` / `Entity.spawn` | every dynamic creation | id minting, placement, `default_entity_vars`, `checked_unit_vars`, on_entity_spawned, summon budget |
| `Entity.remove` | every removal | part subtree, riders, auras, schedules, turn pointer (`turn_vacated`), on_entity_despawned |
| `Match._process_death` | every death | on_death, corpse vs delete, part death routing, turn rebuild, a map vehicle's occupants (`_map_vehicle_died`) |
| `Match.fire_entity_moved` | every completed move | part / aura / rider re-stamp, snake resettle, fog memory, on_map_vehicle_moved |
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
  lists every workspace, `!owner errors` the crash log, §7.16); `!whoami`
  shows the badge. Owner is NOT admin of
  every server: server-wide commands still need that server's Administrator.
- **System rules on load:** a system saved by an older build may name a
  gamerule that no longer exists — `GameSystem.from_dict` drops it with a
  console note; a parent link to a missing system or one closing a loop is
  dropped too (`MatchManager.check_system_links`). A gamerule the save lacks needs nothing: systems store only
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
`systems.json`, `system_history.json` (`!system undo` steps), `audit.json`
(`!server audit`), `matches/<id>.json`, `matches/<id>/history/`
(`index.json` + one `<sequence>.json` per undo snapshot),
`matches/<id>/journal/` (per segment `<n>.json` = its starting state and
`<n>.jsonl` = its entries, appended to; `Storage.write_journals` after each
commit, `_truncate_journal` on a rollback), `saves/`, `sprites/`, `templates/`
(entity templates, outside commits; `templates/.trash/` = their replaced /
edited / deleted versions, 24 h; `templates/prefabs/` the prefabs, with
their own `.trash/`, §7.19), `backups/` (a dated copy of
systems.json, workspace.json, system_history.json and audit.json, written
by each commit that changes one, `<stem>.<unix ms>.json`; kept for the
owner limits `server_backup_days` (7) and `server_backup_count` (200 per
file), pruned after each copy and hourly), `corrupt/`;
plus `data/bot_settings.json` (owner limits + rule ceilings), `data/errors.json`
(the crash log, §7.16) and
`data/.trash/` (wiped data, 24 h). Mechanics:
- **Commit after every top-level command** (`vtt_commands.persist_workspace`,
  called from `CommandRegistry.run` inside the command lock, also after a
  Discord pan click). `Storage.commit(mgr)` serializes every match and the
  workspace / systems and rewrites only files whose text changed (compared
  with `_written`, the text last written); removed matches / snapshots are
  deleted. Each file is atomic (temp + rename); no journal (user-accepted:
  a crash can leave two files of one command a command apart). New files
  are written (history index last) before old ones are deleted
  (`_apply_plan`), so a stop between leaves extra files, never an index
  naming missing snapshots. A snapshot whose file is missing or
  unreadable leaves its history with a console note (at load, in a commit,
  across the `persist_undo_history` switch; `MatchHistory.discard`) instead
  of failing every commit; `commit_all` saves each server on its own.
- **Match files and snapshot files leave out `rules`** (`_disk_state`): it
  was 87% of an empty match file and is rebuilt from the system on every load
  / restore. A snapshot read back from disk therefore has no `rules`; the
  snapshot diff only compares rules when both sides have them.
- **Lazy history:** `Snapshot.state` is a property; a persisted snapshot
  drops its state from memory after the commit (`release_state`) and reads
  `path` on first use. `MatchHistory.index()` / `from_index()` are the disk
  shape. `persist_undo_history` (rule, default on) = history files written;
  off = memory only, files deleted (the commit that drops them first reads
  each released snapshot's state back into memory).
- **Limits** (`Storage._over_limit`): per server (default 1000 MB, the whole
  server folder) and global (20 GB, everything incl. trash), owner-set with
  `!owner limit`. A commit that GROWS past a limit first cuts the server's
  old template versions (`templates/.trash`), then each match's older
  command journal segment (a journal with one segment rolls over first: a
  new segment from the state now, the old one dropped), then its oldest
  autosaves (never manual saves) from matches with
  `storage_trim_autosaves` on (now OR at the last commit, so turning it off
  works over the limit); if that can't make room, `Storage.rollback` puts
  the server back to what's on disk — matches in place via
  `action._rollback_match`, history from the index — and the user is warned.
  A commit that doesn't grow is always allowed. The usage counter follows
  the disk: a commit adds its own files' delta, `write_journals` the
  journal bytes it writes or deletes (the limit check counts both).
  `!store save` / `!history
  export` check room first (`check_room`) and are refused if over.
  `!server storage` lists the server's use by part, largest first
  (`Storage.server_breakdown`: each match and its undo history, templates,
  old template versions, saves, sprites, corrupt files, workspace / systems).
- **Startup:** `storage.open_workspaces()` (bot.py, cli.py, gui.py) loads every
  server folder; a file that fails to load moves to `corrupt/` with a console
  note and the rest loads; unknown gamerules in systems are dropped. Data of
  servers the bot left is kept. On shutdown `commit_all` writes once more.
- **Lost systems** (`Storage._settle_lost_systems`): a match whose system
  didn't load (systems.json unreadable or gone) keeps its system name and
  units untouched. If every such match's file carries a copy of its
  system chain (the `system_load_failure` rule `match_backup`: `_match_text`
  adds `system_backup` to the file), the load rebuilds those systems from
  them (the newest file wins a name, default pointers restored); otherwise
  the server is LOCKED (`MatchManager.recovery`, runtime): every command
  but `!server recover`, `!server backups`, `!help` and `!as` is refused
  (`_runs_while_locked`), and nothing is saved (commit, journals,
  commit_all), so the files on disk stay what a recovery works from.
  `!server recover` (admin) shows the reason, the matches and the options:
  `backup <n>` restores a dated systems.json copy, `accept [confirm]`
  moves the uncovered matches to the default system; both rebuild what the
  match backups cover. After a lock or rebuild the next commit rewrites
  systems.json and workspace.json (864-865). Without a systems.json that
  loaded, `default` is the registry's stand-in (most servers customize
  it), so its matches count as lost too and aren't renamed at load; a
  match backup replaces the stand-in (869).
- **Saved data from older builds loads** (`LEGACY_IDS_OK`, set while
  saved units are read): legacy ids, a clamp bound that is neither a
  number nor a var path (dropped with a console note, the clamp too when
  no bound is left), a var under a now-reserved name (`map`) in a
  template file (`legacy_template_fixes`) or on a match's unit or corpse
  (at match load), moved to `<name>_old` with a console note, a numeric
  team (read as text at match load, corpses too). A refusal added for new input must
  not move old files to `corrupt/` (868). What a load changes reaches the
  command journals: a template it fixed as a `template` entry, a match
  whose loaded state differs from its journal's last hash as a `state`
  entry (`(loaded from disk)`, `Storage._journal_load_changes`), so a
  replay goes on from what the server holds.
- **Backups** (`!server backups [file] | restore <n>`, admin): each file's
  copies numbered on their own, newest first; `restore <n>` puts
  systems.json copy n back through `_restore_systems` (refused if a match's
  system isn't in it), an ordinary `!system undo` step (863); a copy equal
  to the systems now is refused, and the listing marks such copies (874).
  The two logs (system_history.json, audit.json) keep only their newest
  owner limit `server_backup_log_count` (3) copies: they grow with every
  edit and are copied whole. The other
  files' copies are for an operator to copy back by hand. A commit over the
  limit cuts the oldest copies after old template versions, keeping each
  file's newest unless this commit writes a new one.
- **A replaced manager doesn't save:** `persist_workspace` skips a manager
  its workspace no longer holds (after `!as restart`, the old one's state
  would overwrite what was loaded).
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
operators (incl. In/NotIn), And, Or; Break / Continue only inside a `for`
body; Lambda only as the function argument of `map_with` / `filter_with` /
`sort_by` (`_HOF_FUNCS`: one plain parameter, an expression body validated
like any formula, never assignable or storable). **Banned:** Subscript
(except `entity[X]`), Lambda anywhere else, While, comprehensions, Import,
Try, With, class/function defs, AugAssign, chained/tuple Assign. A Call's
function must be a bare Name.

**One namespace dict.** A formula runs with its namespace as globals AND
locals (`exec(code, ns)`, `ns["__builtins__"] = {}`), so a lambda body
resolves names (`__read`, action locals, a `!func`'s parameters) where its
formula does. A local or loop variable named with a leading `__` is
refused (`_check_binding_name`): binding `__builtins__` or `__read` would
replace the engine's. Any formula naming the engine's own `__` helpers
(`__read`, `__write`, ...) is refused at compile (`_refuse_engine_names`,
every parse path), and a `!func` can't take such a name (809). The same
pass rewrites lowercase `true` / `false` / `none` to the constants
(`LOWERCASE_CONSTANTS`, as commands and rules spell them), so those three
can't name a local, loop variable, lambda parameter, `!func`, parameter
or unit (825).

**Two modes.** EXPRESSION formulas (conditions: block / opaque / visibility /
alive / death conditions, watcher conditions, slot conditions, modifier
values, `$()` args) can't assign. PROGRAMS (passives, ticks, watcher effects,
`!func` bodies, status hooks, actions) can, and every program binds
bare-name LOCALS (`dealt = base - armor`; `_collect_program_locals`
pre-collects them as known params, `_EntityAccessTransformer(locals_ok=)`):
a local lives until the formula ends and is never stored (a `!func` call
runs in its own namespace copy), and can't take the name of a function
(built-in or the match's `!func`s), a hook / action binding, an entity
token, `match`, `entity` or a `__` name (886). A rule's schema says which
(`"formula": "expression"|"program"`), and `!system set` validates it.

**Identifier surfaces** resolved at runtime: `_ALLOWED_FUNCS` (pure helpers),
`_MATCH_FUNC_NAMES` (match-bound), `HOOK_CONTEXT_NAMES` (bindings a fire site
supplies in EvalCtx.extras: actor, target, args, changed_key, pov_team,
tile_x, corpse_id, vehicle, slot, skip_status, event_name, ...),
`_ENTITY_TOKEN_NAMES` (self / this / current, and `parent` for a body part),
`known_funcs` (the match's `!func`s), `known_params` (loop variables, and
the locals of every program).

**`entity[X].path`** reads and writes entity vars. X is a literal id, a token,
a known param, an action binding or any HOOK_CONTEXT name; any OTHER bare Name
is read as a LITERAL id (`entity[foo]` = the unit with id "foo"). A name the
program assigns anywhere is its local from the first line, so `entity[b]`
reads the local `b` (before its assignment: "`b` is used before it is
assigned", naming `entity['b']` when a unit `b` exists). The path side
has reserved names (`logic.RESERVED_VAR_PATHS`): `x`, `y` and `name` read the
unit's position / display name, and nothing can be written at or under them.

**Loops.** `for v in <call>` iterates only a `_LOOPABLE_FUNCS` function
(`entities_within`, `parts`, `range`, `each`, `keys`, `team_members`, ...);
`range(...)` gives counted loops and `each(value)` iterates a list or a dict's
keys. `break` / `continue` work inside a loop body. Iterations are bounded by
`formula_loop_limit`, which the higher-order helpers charge per item
(`map_with(items, f)`, `filter_with`, `sort_by(items, f, desc=False)`; `f` is
a `!func` name or a lambda; banned in `$()`, since a `!func` may write).

**Maps.** A coordinate on another map is `[x, y, map]`; unit-less spatial
functions take `map=` (§7.8 "Maps in formulas").

**Values.** Coordinates are LISTS (`[3, 3]`): built-in results are listified
(`_listify`), a tuple literal compiles to a list, stored tuples become lists;
read them with `coord_x` / `coord_y` or `get(c, 0)`. A coordinate goes
wherever a function takes an x / y pair (`formula._coord_lists`, every
built-in with `x, y` / `x1, y1` / `from_x, from_y` / `origin_x,
origin_y` parameters): `[x, y]` fills both, an `[x, y, map]` cell's map
goes to the function's map slot, and a function without one (it works
on its unit's or zone's map) refuses a cell on another map; a call that
fails with a map's name among its plain arguments says where a map
goes (877). `get(container, key)`
reads a list index / dict key / dotted path (subscripts are banned). Stored
values are deep copies with string dict keys (`_own_value`), and only JSON
values (numbers, text, bools, None, lists, dicts) can be stored.

**Resource bounds.** `_ArithGuardTransformer` rewrites `** * + %` into bounded
helpers before every compile (huge ints, huge strings / lists refused;
`formula_size_limit`); a formula is also charged for every list value it
builds (`range`, `each`, list `*` in full; a list `+` the values it adds
plus 1/100 of those it copies, so collecting n items one at a time costs
about n + n²/200, 872) against 10 x
`formula_size_limit`, so a `$()` list of `range()` calls or `map_with` over
ranges stops early; geometry builders charge `formula_cell_limit`, sight
lines `_check_line_budget`; `!func` recursion has its own limit. One bot
process serves every server, so a formula's cost must follow the map, never
its arguments.

**Action mode** (`action_mode=True`) adds: `source.<path>`, `args.<key>`, `target.x` etc. (any bare-Name root)
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
  keeps live bindings per `undo_channel_bindings_mode`, keeps `paused`, the
  RNG (seed, position, live generator) and the command journal, and
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

**Reports from another LLM.** The user sometimes runs a separate,
read-only LLM for bug-finding passes and forwards its findings. Treat each
finding as a bug report: reproduce it (a probe or a scenario) against
current main before fixing, say which findings didn't reproduce and why,
and ask the user when a finding is a design question rather than a defect.
Fix confirmed ones like any bug (scenario, regression, the relevant
harness from the toolkit below).

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
- **Formula-text leak probe** (950): a board with a marker string in
  every place a GM stores a formula (functions, unit / global / team /
  default passives, status ticks / hooks / `_unless`, watchers, actions,
  tile and zone hooks and block / opaque, macros, schedules, formula
  rules, system libraries, templates, prefabs, aliases); every root x
  read subcommand x an argument pool run as a player under
  `formula_text_visibility host`; any marker in a reply is a leak (alias
  expansions excepted, by design).
- **Existence-oracle detector** (41): the same board; each command run twice
  as the player, once naming a hidden unit (as an arg, `near:<id>:60`,
  `for=<id>`, `to=<id>`, `<id>.hp`) and once naming a nonexistent id; any
  reply difference after swapping the names is an oracle (the grep detector
  misses a reply that names no secret but behaves differently).
- **Formula-function fuzzer** (30): every function × wrong-typed /
  missing-entity / wrong-arity args under a 1 s SIGALRM; flag raw Python
  messages after "Runtime error:".
- **Command fuzzer** (30): every root × subcommand × junk / ids / numbers /
  paths / `$()` as host (~100k runs), flag 💥 / Runtime / timeouts; point
  `vtt_commands.SAVES_DIR` at a temp dir first.
- **Huge-number timing fuzzer** (34): 10⁹ / -10⁹ in every numeric position of
  every command and every state-changing function, under SIGALRM.
- **Replay differential** (journal): run every scenario, export each
  match's journal, replay it into a fresh workspace, compare the final
  state hash and the replay's own divergence report (808/808 journals
  matched when it shipped). Pair it with a memory-equals-disk check of
  the journals, system_history.json and audit.json after every command.
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
- **Template round-trip fuzzers** (37): random writes / deletes / gives /
  detaches / copies on nested partial containers, asserting that the saved
  file form materializes back to the unit's data, and that a resync after a
  random library edit equals the pre-edit save re-materialized; plus
  save → spawn → save idempotence on random multi-tile units with parts.
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
- **Rule-change chaos** (40): the stateful chaos harness plus random
  `!system set` of any rule to any valid value mid-game; disk == memory
  checked after every step (caught persist_undo_history losing snapshots).
- **Rule-value fuzzer** (40): every rule x each enum choice / bool / int
  bound / formula / junk value, then a fixed suite touching every subsystem;
  flag 💥 / Runtime and "saving to disk failed" printed to the console.
- **Big-map timing** (40): per-command time after whole-map tiles, reveals
  and explored fog on 500x500, and far-off coordinates (10⁹) in every
  placement search.
- **Usage drift** (doc 2, W3): after every commit + journal write of every
  scenario, the server's usage counter equals its folder's bytes on disk.
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
- **Large features** (a subsystem spanning several PRs, like multiple maps):
  settle about 95% of the design in discussion BEFORE any code, written
  down in a design doc (`docs/multi_map_design.md` is the model), then build
  from it. The user prefers this over many questions mid-implementation:
  the multi-map build needed only three follow-up questions, each about a
  gap the doc hadn't reached.

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
- **Var-name rules** (`VAR_NAME_RULES`: hp / max_hp / turnorder / team /
  part_name / footprint width + height / atb_charge / disguise /
  fog_vision_radius / overlay `_var`; each value one plain var name, an
  identifier that isn't a keyword, x / y / name or `__`-prefixed,
  `check_var_name_rule`): each unit remembers the names it uses
  (protected `Entity.var_names`, saved with it, its corpse and its
  templates). `Match.sync_var_names` / `rename_unit_vars` move a unit's vars
  to the names its match's rules give, all at once (no var hooks, clamps'
  paths and bounds follow, a partial marker records the move), at
  `Entity.spawn` (templates, summons, copies, revives from other names),
  in `apply_statblock` (a form captured under other names) and wherever a
  match's rules are rebuilt (`MatchManager.apply_system_rules`: a system
  edit, load, undo / restore, store load; corpses too). A var already under
  the new name follows `var_name_collision` (tracked default / existing /
  refuse; refuse pre-checks every unit and corpse and changes nothing).
  `!system set` of such a rule (`_rename_system_var`) also renames the
  system's default_clamps / default_entity_vars and the engine formula
  rules still in their default text (death_condition, the kill / revive
  effects, atb_charge_formula; a GM's own formulas are left as written),
  and refuses a name another var rule already uses (741-742).
- **`Entity.write_var` / `remove_var`** are the only way to change vars:
  vital vars are coerced to finite whole numbers or refused, the team var
  is stored as text (`team_text`: 1, 1.0 and "1" are one team, true is
  "true"; a list / dict refused, nothing nests under it; also in
  `checked_unit_vars`), nothing nests
  under a vital, vitals can't be deleted (`allow_protected` only for the
  property setters), reserved paths (x / y / name) are refused, a footprint
  write must fit (`check_body_fits`), then clamps, var hooks and the death
  check run. Wholesale var sets (spawn, summon / part / segment templates,
  transform) run `checked_unit_vars` instead.
- **Every store** (unit vars, team data, match vars, tiles, zones, statuses)
  goes through `_own_value` (deep copy, string dict keys, JSON values only),
  `check_store_path` (no empty path segment) and `check_no_value_ancestor`
  (no `k.x` while k = 5). Status number fields (level, duration) via
  `checked_status_value`. A stored value is bounded: nesting depth
  `STORE_DEPTH_LIMIT`, total items `STORE_SIZE_LIMIT`, integer digits
  `STORE_INT_DIGITS` (a value past them couldn't be saved or printed).
- **Unit ids** follow `ID_RE`; `RESERVED_IDS` (self / this / current /
  parent) and ids `entity[X]` would read as something else
  (`unreachable_id`: hook and action bindings such as target / actor /
  rider, Python keywords, True / None) are refused at creation
  (`Entity.__post_init__`) and as template names; `mint_entity_id` skips
  them. A digits-only id works: `entity[123]` reads unit "123". Ids that
  aren't identifiers (`orc-1`) are read with `entity['orc-1']`.
- **`default_entity_vars`** (`!defvar`) fills MISSING vars at spawn, before
  vital validation; `!ent add` / template values win.
- **Clamps** (entity + system `default_clamps`, `!clamp` / `!gclamp`; a
  bound is a number or a var path wherever a clamp arrives, `ClampSpec`): hard
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
  one all-or-nothing undo step (684); the unit's death check runs once,
  after every var is written (`_death_check_deferred_ids`), and a rollback
  drops the lines its writes surfaced. `!ent set_var` coerces only lowercase
  `true` / `false` to bools and keeps `inf` / `nan` as text.
- **Container helpers** (the engine has no inventory concept): `var_add`,
  `var_move` (looting), `var_copy`, `var_sum_field`, `item_add` /
  `item_consume` (drops the container at 0; `amount_field` rule; `var_move`
  onto its own path is a no-op, into its own subtree refused), `var_keys`,
  `var_sum`, `var_clear`, `get`, `keys` / `values` / `index_of` / `unique`
  (547-548, 685-686); list surgery: `take` / `drop` / `slice` (0-based,
  negative counts from the end), `remove_at` / `insert_at` (new lists),
  `reverse`, `flatten` (one level), `zip` (pure, loopable, charged to the
  list budget, 887). keys / values / each / len / pick / shuffle /
  roll_table skip a partial template's `__template` marker
  (`formula._data_items`), as var_keys skips every `__` key. Authoring notes: in an item's action `source.<path>` is
  the ITEM's container and `entity[self]` the HOLDER; dict keys are unique, so
  identical items stack through an amount field; `!ent set_var hero
  inventory.potion 99` replaces the whole item dict.
- **`!ent clone`** copies the whole part subtree, strips mount links, refuses
  a body part, and is all-or-nothing (`_rollback_match` on any refusal).
- **Summons** (`Match.summon_entity`): `summon(template, x, y)` /
  `summon_near(..., radius)` / `summon_from(path, ...)` turn an entity-shaped
  dict into a unit (id minted from its name, footprint placement, parts /
  segments keys, on_entity_spawned), bounded per command by
  `summon_event_limit` (free-cell searches walk only the on-map cells of
  each Chebyshev ring, `_ring_radii` / `_ring_cells`); `entity_snapshot(eid)` and `!ent
  store_entity_into_var` make such a dict (identity and links stripped:
  id, x, y, `part_of`, `mounted_on`, `mount_slot`; a summon also drops
  `__follows`, so a snapshot of a rider or a segment summons a free unit);
  `remove_entity` despawns. A snapshot (`capture_statblock`, also the
  transform stash and a transform into a live unit's form) holds the
  whole part subtree, segments included, at offsets from the anchor, so
  a summon or transform rebuilds the body around the new unit
  (`_apply_statblock_parts`; a part id under the unit's follows the new
  unit's, another is kept or minted anew, 830). Server templates (§7.17)
  build on the same
  path.
- **Occupancy:** one unit per cell, except units with the `__cell_stackable`
  var; `cell_entity` names the blocker, `entities_at` everyone covering a cell.
- **Unit cap:** `max_entities_per_match` (-1 = none, the default) bounds
  the units a match holds (parts and segments count, corpses don't):
  `Match.check_unit_room` at `Entity.spawn`, and whole bodies are checked
  before their first unit spawns (`_body_count`: `summon_entity`,
  `spawn_template`, a revive with its stored parts, a transform's growth,
  `copy_entity` into the destination, `_apply_statblock_parts`), so no body
  arrives without its parts (891).
- **Shields / temp hp:** named absorb pools under the roots in
  `temp_hp_sources` (default `shields`; per-unit `__temp_hp_sources`), each
  `{amount, priority, tags, not_tags}` or a number (`tags` a tag filter,
  §7.20). `absorb_damage(eid, n, tags)` drains matching pools highest
  priority first and returns what
  penetrates (the GM applies it to hp); `shield_total`. Decay is GM-composed
  (440).

### 7.2 Passives, hooks, events, watchers

- **Passives** (`Passive`, `!passive` per unit, `!gpassive` global, `!team
  passive` per team, `default_entity_passives`): `when` is a HOOK_NAMES entry
  or `event:<name>`. Every fire site runs `Match._ordered_passives(target,
  pred)` (via `_run_ordered`): the global, the target's team and its own
  passives in one list, highest `priority` first (`Passive.priority`, any
  finite number, default 0; `priority=` on every passive add, `!passive /
  !gpassive / !team passive / !defpassive priority`), ties in the old order
  (globals, team, own, each as added). An own passive is skipped once its
  unit is gone (ghost firing, §5) (882); once a passive removes or kills
  the hook's unit, the rest of that hook's passives, global and team
  ones too, don't run on it, while a hook fired for a unit already gone
  (on_death, a despawn) still reaches its globals (937).
- **Var hooks** (`on_var_*`, `on_var_written`, `on_var_write_attempt`): run
  bottom-up for subtree writes; `self` is the CURRENT-TURN unit, and the
  extras carry changed_key / old_value / new_value / intended_value /
  was_clamped. Status hooks bind `self` = the affected unit. Recursion capped
  by `var_hook_recursion_limit`; `var_hook_warning_verbosity` (off / minimal /
  detailed).
- **Turn / round hooks** fire for the acting unit; its attached parts fire
  only their OWN passives (`own_only_targets`). Movement hooks per footprint
  cell (`fire_footprint_tile_*` / `fire_footprint_zone_*`) plus
  `on_entity_step` per step; a hook that moves units again nests at most
  `move_hook_recursion_limit` deep, and one chain (an outermost move and
  every move its hooks set off) makes at most `move_hook_event_limit`
  (500) moves with hooks (`move_hook_level`, counted in
  `fire_entity_moved`; past either, moves fire no hooks, with one ⚠️ per
  limit per chain in the reply and on the console; re-stamps always run;
  each `transfer_entity` hop counts for depth) (875). Lifecycle: on_entity_spawned,
  on_entity_despawned (every non-death removal, fired BEFORE the unit
  leaves; `Entity.remove(despawn=)` returns its log), on_death / on_revive,
  on_entity_moved, on_entity_changed_map, on_map_vehicle_moved / on_map_vehicle_death (§7.8), on_mounted / on_dismounted, on_turn_skipped (`skip_status`), on_status_*,
  on_action_used / on_action_failed / on_action_used_on_target.
- **Event bus:** `emit(name, payload, target)` / `!emit`. Global handlers
  fire ONCE (self = target, else the current unit); a directed event also
  fires the target's team + own handlers. Payload via `event_get` /
  `event_has`; `event_name` binding; `event_recursion_limit`.
  `declare_winner` emits `match_outcome` (winner / reason / round) — the bus
  was chosen because `fire_hook` runs globals once PER TARGET unit
  (437-439, 568).
- **Watchers** (`!watch`, `Match.watchers`): an expression condition +
  program effect, EDGE-triggered (false → true), polled after each
  top-level command settles and re-checked until a pass fires nothing
  new (a chain settles in the command that started it and shares its
  undo step), at most `watcher_chain_limit` passes (a ⚠️ when it stops a
  loop; the rest of the chain waits for the next command that CHANGES the
  match, `_watcher_backlog`: a read doesn't run it, and `!match replay`
  doesn't poll); `once` removes after firing; `last` serialized; a malformed
  condition reads as not met (427-428, 826).
- **Output:** every formula-driven change hands its log to
  `Match.surface_log` (action buffer, else the command's `FORMULA_LOG_SINK`),
  shown at the end of the command in order (651).
- **Schedules:** `schedule(delay, ...)` (round-based; raises under ATB),
  `schedule_on(eid, ...)` (turn-based), `cancel_schedule`; a removed unit's
  schedules go with it. Read with `schedules_of(eid=None)` (names, loopable)
  and `schedule_left(name)` (turns / rounds left, None when gone) (766).

### 7.3 Turns, rounds and ATB

- **Turn order:** sorted by `turnorder_var` per the `turnorder_*` rules
  (direction, `turnorder_tiebreak_var` / `_direction` (a GM var such as
  `dex` or `stats.agi`; a unit without a number there goes after), then
  tiebreaker incl. `random_stable`, team grouping, change policy
  immediate / deferred). A write to the tiebreak var and a changed
  `turnorder_*` rule (`apply_system_rules`) re-sort it like an initiative
  write (770). Before the first `!turn next` the pointer sits at
  the top of the order.
- **`!turn set <id>`** (`Match.set_turn`) is a turn change: the acting
  unit's turn ends (turn_end hooks, ticks), <id>'s starts (turn_start,
  schedules, the turn autosave); under ATB <id>'s bar resets as if it had
  filled; before the first turn it starts round 1 at <id>. `!turn set
  <id> quiet` only moves the pointer (53, 827).
- **`!turn next`** (`Match.next_turn`): turn-end hooks / ticks → advance (round
  wrap fires round hooks, ticks, `round_start_message_format` line) → skip
  units whose statuses carry `skips_turn` (each skip fires on_turn_skipped
  and the unit's turn_start / turn_end status ticks, as under ATB, so a stun
  counting down on its own turns wears off; `_skip_to_eligible` tracks the
  ids it has seen, so removals mid-pass can't over- or under-run it). When
  every unit is skipped, whole rounds keep passing until one can act, up to
  `skip_all_round_limit`, stopping early once a round changes no status
  (permanent stuns) → `_start_current_turn`. The reply prints hook
  output first and the `turn_start_message_format` line last (unit
  placeholders, `{short_id}`, `{round}`; `turn_start_hidden_message_format`
  for a unit the channel's view can't see; empty = no line) (770).
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
  resume` runs them (`resume drop` discards); held commands are saved with
  the match (`paused.held`), so a restart keeps them; never an undo step;
  `!dry` runs during a pause (637-638, 822).
- **Reminders** (`!remind [secret] <n> <text> | list | remove <id>`;
  `Match.reminders` / `reminder_seq` / `held_reminders`, saved, undo
  state): due at `turns_elapsed` + n, fired by `_begin_turn`
  (`_fire_reminders`). A public one's line (`reminder_message_format`)
  goes out through `surface_log` with the turn output; a secret one waits
  in `held_reminders` and ends the reply of the next command a host runs
  on the match in a channel without a team POV (a POV-bound channel is
  one players read; `CommandRegistry._run`, after the handler, inside that
  command's undo step; `!as` doesn't count). `!remind list` shows the
  secret ones only to a host in such a channel (893, 903).

### 7.4 Statuses

- **Instances** live in `entity.status[name]` (level, duration, any data);
  raw editing via `!ent status`. **Definitions** (`Match.status_definitions`,
  `!status def/tick/when/stack/maxlevel/data/tags/removes/blockedby/sprite/
  drop/list/info`; a system holds its own, `!system status`, copied into a
  new match, §7.16) describe a status ONCE; an instance resolves behaviour from
  the definition of the same name. Duration decrement and self-removal live
  in the GM's tick formula; the engine forces no decay (390-391).
- **Definition hooks** (`!status hook <name> <on_added|on_removed|on_changed>
  [formula]`, the definition's `hooks`; `logic.STATUS_DEF_HOOKS`): run by
  `fire_status_event` after the on_status_* passives, self = the bearer,
  while the unit exists. Removal hooks (passives too) bind `removed_by`:
  `dispel`, `transfer`, `removes` (another status's `removes`), `expired`
  (a counter reached 0, or the status's own tick removed it:
  `logic.STATUS_TICKING`) or `removed`; `_emit_status_diff(removed_by=)`
  carries a caller's cause (884). The formula status functions surface
  the hook output they set off. A hook that changes a status fires hooks
  again: `fire_status_event` counts the nesting (`_status_event_depth`)
  and past `status_hook_recursion_limit` (32) the rest of the chain fires
  no status hooks, with one ⚠️ (900).
- **Applying** (`apply_status` / `status_apply` / `!status apply`): stacking
  from the definition's `stack`, else `status_default_stack` (refresh /
  add_level / extend / replace / none). `max_level` is a hard ceiling on
  every write of a level: applications (`_cap_status_level`), every status
  change through `_emit_status_diff` (`_cap_live_status`: status_set,
  counters, `!ent status set` report the stored value), statuses a spawn /
  summon / transform brings (`_cap_unit_status_levels`); lowering it with
  `!status maxlevel` lowers live instances as status changes (740).
- **Rename / copy:** `!status rename` (`Match.rename_status`: the
  definition, live instances on units, parts and corpses (their parts
  too), other definitions' `removes` / `blocked_by`, and `status_immune` /
  `status_resist` / `__status_immune` / `__status_redirect` anywhere in
  units' vars, tracked for partial templates; no status hooks; formulas
  naming it as text are listed in a ⚠️). `!status copy`. Zones, tables
  and macros rename the same way (`_rename_entry`; `_mentions_note` scans
  `_match_formula_slots` and macro bodies for the old name); `!macro copy`,
  `!tile def copy`, `!passive copy <eid> <pid> <dest> [new_pid]` (767).
- **Prims:** `status_apply` / `status_force` / `status_get` / `status_set` /
  `status_has` / `status_tags` / `status_has_tag` / `statuses_with_tag` /
  `status_resist_of` / `is_status_immune` / counters / dispel / transfer.
- **Tags and tokens:** a definition's `tags`; a token is a bare name or
  `tag:<x>` (so a status name can't start with `tag:` or hold a comma,
  `check_status_name`) (`_status_token_matches`), used by `removes` / `blocked_by`
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
- **Action restrictions** (#234, `logic.ACTION_RESTRICTION_FIELDS`):
  `forbids_actions_with_tag` / `forbids_actions_with_name` (the bearer
  can't use such actions) and `immune_to_actions_with_tag` /
  `immune_to_actions_with_name` (such actions can't target the bearer or
  its parts), each with a `<field>_unless` expression lifting only that
  list while true (self = the bearer; actor, action_name, action_tags,
  target, status_name bound; a formula that fails keeps the restriction
  and the refusal names the error). `!status <field> <name> [words]`
  (`-` clears), `!status <field>_unless <name> [formula]`; an instance
  holding the key replaces the definition's (`Match.status_field`, the
  one read path, where per-level data, idea #259, would merge in).
  Innate or equipment restrictions are a permanent status. Checked in
  `run_action` (§7.6); `!system lint` covers the formulas.
- **Display:** status sprite overlays (§7.13); the `{statuses}` roster
  placeholder (`vtt_commands._statuses_text`, in the default
  entity_info_format): each status through its definition's `display`
  format (`!status display`) else the `status_line_format` rule, its
  instance fields as placeholders (885).

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
  more% product, set last, min floor, max cap), HIGHEST tier first (the
  engine-wide priority rule: passives run and shield pools drain highest
  priority first too; the default op offsets run add 500 … max 0, so the
  default order stays add, inc%, more%, set, min, max, 407, 883),
  `modifier_op_order` breaks ties; `modifier_stat_caps` clamps the result.
  An unknown op folds as add and `!mod show` flags it.
- Pure query: the engine never applies modifiers; the GM threads
  `apply_mods(...)` / `list_mods(...)` through their own formulas. `!mod show
  <eid> <stat> [base] [tag ...]` (399-400, 406-408, 480) also lists records
  left out for an unreadable condition / value / priority. A non-finite base
  or folded result (stacked more% overflow) is refused with a warning naming
  the records (user call).

### 7.6 Actions and choices

- **Actions** are data at `vars.<container>.actions.<name>` (`target` type:
  entity / location / entity_list / location_list / none / corpse /
  corpse_list; `body` program). `discover_actions` walks containers
  (`action_container_mode` / `_paths`); mount slots add their own (§7.10).
  `!action <eid> <name> [target] [k=v ...] [answer=...]`, `use_action`,
  `has_action`. A list target lists each unit / cell / corpse once: a repeat
  is merged with a ⚠️ (user call: to act on one twice, run the action
  twice; `dedupe_targets`). A location target must be on the map and is a `Coord`
  (`.x` / `.y` / `.map`) that stores as [x, y]; a typed `map=<map>` names
  the map of a location / location_list target's cells (the actor's by
  default; not an action arg there, 880); `use_action` coerces its target to
  the action's type the way `!action` parses it (`action.coerce_target`:
  [x, y] → location, ids checked, lists non-empty).
- **`run_action`** is transactional: pre-state captured, body run,
  `fail()` / an exception rolls the match back in place
  (`_rollback_match`: serialized state restored, runtime fields and undo
  history preserved). A failing NESTED action rolls back only its own
  effects (check `use_action`'s result to abort the outer one).
  `action_recursion_limit`. `cmd('...')` dispatches commands on
  `action_cmd_allowlist` (default `ent`) through `_BufferCtx`, which carries
  the channel and identity of the command that ran the action
  (`_BufferCtx.for_ctx`), so target-host checks (`!ent transfer` / `copy`)
  apply; action bodies can't run admin commands (no admin flag), and save
  a template only where `template` is allowlisted and the runner may save.
  A `cmd()` or `transfer_entity` that can change another match snapshots
  the other matches first for every enclosing action level
  (`_CROSS_LEVELS`), so a failed outer action restores them too (812, 818,
  856). Writing under an `actions` var needs a host (`!ent set_var` /
  `set_vars` / `delete_var`), so a player can't author an action body.
- **Action tags** (#234, 916-918): an action's own `tags` stay with
  it; a container's `action_tags` (any dict above the action: the vars
  root, an item, an `actions` dict, a mount slot) reach every action
  below it; `exclude_inherited_action_tags` (`all` / `*` or a list) on
  an action or a container cuts tags arriving from above, for everything
  at and below it. Discovery carries the (tag, source) chain down the
  walk (`action._inherit_tags`); `Action.tags` is own first, then each
  container nearest first, with `tag_sources` and `tag_problems` (a field
  that isn't a tag list: only its usable words count, `!action info`
  names it). `!action info` lists the tags and their sources, `!action
  list` marks forbidden actions (🚫 status on unit). The body, the
  on_action_* passives and the `_unless` formulas bind `action_tags`;
  reads `action_tags_of(eid, name)`, `action_forbidden_by(eid, name)`,
  `immune_to_action(target, tags, name=None, actor=None)` (for bodies
  that pick their own victims, e.g. from a location).
- **Restrictions** (`_restriction_refusal`, in `run_action` before the
  body, so `!action`, `use_action` and rider actions all pass it): the
  status fields of §7.4 on the actor, its body root and, for a mount
  action, the rider and vehicle with their roots
  (`Match.action_forbid_units`) forbid it; those on an entity target and
  its body root (`action_immune_units`) make it immune. A refusal is a
  clean fail (`forbidden` / `immune`, on_action_failed fires, use_action
  returns False) worded by `action_forbidden_message_format` /
  `action_immune_message_format`; an entity_list target drops its immune
  units with a ⚠️ naming each, refused only when none is left. Corpse
  targets aren't checked for immunity (§8). Then the action's
  `target_requires_tags` / `target_refuses_tags` filters (§7.20) against
  the target's tags, a corpse's included (reason `target_tags`,
  `action_target_tags_message_format`, list targets drop the misfits the
  same way; a filter that doesn't parse refuses every target and `!action
  info` names it).
- **Choices:** `choose(prompt, options)` / `choose_number(prompt, lo, hi)`
  replay the body: on an unanswered choice the attempt rolls back and re-runs
  with answers replayed in order (`answer=` tokens, else the surface's
  `prompt_choice`; the harness must pre-supply; answers the body never asks
  for are named in a ⚠️, user call). Per attempt the RNG, output
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
  matches removed, deleted ones restored as the original object even when
  a line made a new match under the id, pointers and rules restored;
  bot-wide settings are not; a line that crashes undoes like a ❌, and a
  deleted match comes back with its history, saves and journal) (692-693,
  816). **`!run <file>`** reads a saves-folder file (admin). Both nest
  within `macro_recursion_limit`.
- **Lines of `!batch` / `!foreach` are gated** (`_GATE_LINES`, checked in
  `dispatch_no_snapshot`): each typed line passes the per-match gate as its
  caller, so an access override opening the container opens only the
  container; a macro body, action `cmd()` lines and an approved request
  aren't gated again. Nested batch / foreach runs (`_run_container`) count
  toward `macro_recursion_limit` with macro runs, and the nested runs of
  one top-level container share `macro_step_limit` (one step per nested
  run: a sweep rerunning itself per unit fans out); either limit is
  reported once and the rest of the nested runs stop quietly (818).
- **Macros** (`!macro set/run/list/show/remove`): `$1..$N`, `$@`, `$#` (the
  current `repeat` iteration) (single-pass substitution); a `$N` past the
  args given becomes empty text, with no refusal (user call): an optional
  arg is tested with `if "$3" != ""`, and a line that needed it fails after
  the earlier lines ran; `${N:default}` / `${name:default}` give defaults,
  and a `key=value` run arg is NAMED when the body uses `$key` / `${key...}`
  (`_macro_args`; otherwise it stays positional, user call), while a `$name`
  with no such arg is left as written (so `!foreach`'s `$id` inside a macro
  works); control flow `if / elif / else / repeat / end`
  with read-only conditions (a negative `repeat` count is refused);
  `macro_repeat_limit`, `macro_step_limit` (counts
  dispatches AND loop iterations, shared by nested runs through
  `mgr._macro_budget`), `macro_recursion_limit`; an `if` whose condition
  can't be read stops the macro; one undo step
  (425-426, 544-546, 555-556, 817). Each macro records its author
  (`Match.macro_authors`); a body a non-host wrote runs its lines gated as
  the runner, like a typed batch (855). A non-host can't replace, rename
  or remove a macro a host wrote (or one with no recorded author), and
  `!macro list` / `show` mark a macro a non-host wrote (871). Aliases and
  `!func`s record their author the same way (`alias_authors` /
  `func_authors`, `vtt_commands._AUTHORED`): a non-host can't change a
  host's, and listings mark a player's (878).
- **`!foreach <selector> ; <cmd> [; <cmd> ...]`**: the `!find` selector
  grammar; `$id $name $x $y $team $map $i $n` substituted single-pass; targets
  resolved before any command runs; one undo step. Host-gated, EXCEPT that a
  sweep whose every inner command is player-available downgrades to `all`
  (`_foreach_read_only`, default-deny: aliases, `$` in the command position,
  self-dispatching commands and elevated args keep it gated). A non-host's
  sweep skips units their POV can't see (486-490, 557-558, 564-565).
- **`!find`** selectors: `var<op>value`, `status:`, `group:`, `action:`,
  `map:`, `near:<eid>:<r>` (a unit the query POV can't see reads as
  missing; only its map), `within:<x>:<y>:<r>` (on one map, §7.8), a
  leading `!` negates, `|`
  alternatives (an alternative that doesn't parse reuses the previous kind and
  key); `show:<csv>` (incl. `status`), `sort:<var>[:desc]`, `count`, `ids`
  (536-538, 631, 694-695).
- **`!assert [--as eid] "<expr>" [msg]`**: read-only; a failure raises
  `AssertionStop`, and batch / run / foreach / macro stop on the `ASSERT_STOP`
  sentinel; in an action, `cmd('assert ...')` fails the action (601-603).
- **`!dry <command ...>`** (hosts only) runs a command, shows its replies and
  the diff of the channel's match, then restores every match
  (`_snapshot_all_matches` / `_restore_all_matches`, the `!batch strict`
  machinery) and each match's RNG state. `_DRY_ACTIVE` makes
  `dispatch_no_snapshot` refuse, also inside a batch / macro / foreach it
  runs, what a match rollback can't undo (`_dry_refusal`): server-wide
  edits, files, template writes, undo / history (kept across a rollback),
  approvals, `!match delete/pause/resume`, a nested `!dry` (768).
- **`!again`** reruns the caller's last top-level command in the channel
  (runtime-only `mgr._last_commands`; never records `again` / `as` / clicks;
  refused inside dispatch_no_snapshot; always player-available) (639, 646).
- **Aliases** (`!alias`, per match; system aliases via `!system alias`)
  resolve BEFORE the access gate; an alias can't take a command's name
  (refused at def / promote, ignored if saved). Command roots and aliases
  are case-insensitive (`!Ent`, on Discord too). `!func` defines formula functions
  (`formula_function_recursion_limit`). `!eval [--as eid]` runs a formula;
  `!eval --trace ...` lists, in order, the formula's own `entity[x]` /
  `match.` reads (`FormulaEngine.trace_reads`, so engine-side checks such
  as the death condition stay out) and every var write / delete the eval
  caused, hook writes marked ↳ by `_var_event_depth` (`logic.VAR_TRACE`,
  `record_trace` in `write_var` / `remove_var` and the match-var writers;
  at most 1000 entries, 60 shown) (894).

### 7.8 Geometry, movement and blocking

- **Maps** (`Map`; `docs/multi_map_design.md`, steps 1-2d built): a match
  holds its grids in `Match.maps` (name -> Map, in creation order, saved
  as `map_order` since files sort keys; a new match has one, `main`,
  `DEFAULT_MAP_NAME`). A Map is the grid alone: size, tiles, zones (stored
  per map), fog memory (`explored`), reveals and map vars, with the
  methods that need nothing else (`in_bounds`, `tile_*`, `zone_*`,
  `zones_at`, `to_dict` / `from_dict`). Units stay in the match's one pool
  and name their map in `Entity.map` (set by `spawn(..., map_name=)`;
  parts, segments, revived corpses and clones take their parent's /
  corpse's / source's map). Match code that needs units takes the Map it
  works on: `mp` params, resolved by `map_of(e)` (a unit's map),
  `get_map(name)` (None = the only map; a match with several refuses,
  so a call site that forgot its map fails loudly instead of using the
  wrong one), `_map_for(mp, e)` (given, else the unit's, else the only
  one). Occupancy, sight, fog, blocking, hooks and rendering all filter
  units by map; a team sees a map only through its units on it. Corpses:
  `find_corpse(id)` -> (x, y, corpse, map) on any map, `all_corpses(mp)`
  one map, `every_corpse()` all of them. Templates, statblocks and summon
  snapshots strip `map` like x / y. Formulas: §7.8 "Maps in formulas".
- **Maps in formulas** (step 2b): the unit-less spatial functions
  (`formula._MAP_ARG_FUNCS`: tile_*, cell_entity, entities_at / in_* /
  on_los, cells_in_*, free_cell_near, in_bounds, grid_width / height,
  has_los, can_see, team_sees_cell, cell_blocked, clip_cells, ...) take
  `map=`: a map name, `view` (the command channel's map, through the
  `logic.COMMAND_VIEW` context var `_run_top` sets; refused outside a
  command) or `@<unit>` (that unit's map; `map_of(unit)` reads the same).
  The call runs on that map (`_map_stack`, read by `_cur_map()`).
  Without it a formula uses its hook's map (tile_map / zone_map), else the
  match's only map; on a match with several it refuses, naming them (no
  fallback on `self`). A cell result on a map other than the formula's
  own is `[x, y, map]` (`_tag_cells`). `random_cell` / `random_free_cell`
  also take a list or `all` (one pick across those maps, every cell
  equally likely, always `[x, y, map]`; a zone names its own map).
  Summons (`summon*`, `summon_template*`), `move_entity(eid, x, y,
  map=)` / `move_entity(eid, coord)` and `transfer_entity` take the map
  in the slot after the coordinates (after the radius for the `_near`
  forms; `summon`'s id prefix only as `id_prefix=`, 877); a summon defaults to
  the hook's map, else `self`'s, else the only one; `move_entity` to
  another map is a cross-map move (returns `[x, y, map]`; a passenger is
  refused and a driver moves its vehicle, as `!ent tp`, 872). `coord_x` /
  `coord_y` read triples, `coord_map` names the map (None for a pair);
  `nearest_cell` / `face_toward` / `clip_cells` honour a triple's map;
  across maps `can_see` / `has_los` are False and `nearest_cell` None;
  the coordinate wrapper answers no relation for cells on two maps (or a
  cell on another map than the function's unit / zone):
  `formula._CROSS_MAP_NO_RELATION` (distance / angle / direction_to None,
  has_los / occupies / cell_in_zone False, the line searches []); the
  writers and reads with no such answer refuse; an untagged pair is on
  map= or the formula's own map (936). A
  location target is a `Coord` with `.map` (the actor's, or a triple's
  map via `use_action`; stored as `[x, y, map]` only when it isn't the
  actor's); a body passes `target` whole to a move or a summon, since
  `target.x, target.y` drop its map. `entity_center`, `aoe_origin` and
  `footprint_cells` give `[x, y, map]` for a unit on another map, as
  `corpse_at`. `maps()` (loopable), `map_var_get / set / del / keys / has`
  (dotted paths on a map's vars, `map=` or the context map; a setting var
  takes what its command stores, `logic.map_setting_value`). `map` is a
  reserved var path like x / y / name: `entity[x].map` reads the unit's
  map and no var can take the name (831-836).
- **Map management** (`!map new <name> <w> <h> | list | delete | rename |
  copy | use | var`, `_map_manage`): names follow `check_map_name`; the
  `!map` subcommand words plus all / view / clear / full / none are
  reserved (`MAP_RESERVED_NAMES`, drift-guarded), so `!map <name>`
  renders that map. `match_cell_limit` (ceiling) bounds the summed area
  (new, copy, a growing resize). `delete` follows `map_delete_mode`
  (refuse lists the units; kill kills them in id order, then drops tiles,
  zones, corpses, fog and channel state naming the map, and from wreck
  records; a carrier dying on a map being deleted ejects no one onto it);
  the last map can't go, nor one whose deletion would destroy every other
  map (`_maps_lost_with`; maps destroyed with it are named). `rename` rewrites every stored `[x, y, map]` list naming it
  (`_rename_map_coords`: unit vars, corpses, match vars, team data, tiles,
  zones). `copy` takes size, tiles (no corpses), zones and map vars,
  renaming each zone `<zone>_<new>` and leaving auras with their units.
  Zone names are unique across the match (`Match.zone_map`, `find_zone`
  and the zone wrappers find a zone's map by name; creating one needs a
  map on a match with several) (782-785).
- **Map vars and settings:** `!map var set|get|del|list` (free-form,
  dotted). Every per-map display setting IS a map var: fog, fog memory,
  background (+ mode), borders (+ colour, opacity), hidden layers, named
  by `MAP_VAR_NAME_RULES` (`map_fog_var`, ...); a map without the var
  reads the old rule live (`fog_enabled_by_default`, `show_borders`, ...,
  `Match.map_setting`). Each map remembers its var names (`Map.var_names`,
  stamped at creation by `fill_default_map_vars`); a rule change moves
  the var on every map (`sync_map_var_names`, `var_name_collision`;
  `_rename_system_map_var` refuses a shared name or, under refuse, a taken
  one, and moves the `default_map_vars` key). `default_map_vars` (MERGE
  rule, `!defvar map`) fills a NEW map's missing vars (786-788).
- **Which map a command means** (`vtt_commands._resolve_maps`): `map=`
  (a name, `a,b`, `all`, `view`) > the unit the command names > the only
  map > the channel's map (`Match.channel_map`: `!map use <name>`, else
  the followed map). `strict` callers (tile edits, zone creation,
  `!reveal_fog`, setting writes, `!ent add`, `!template spawn`) refuse the
  channel's map with the map list and syntax, `!map use` included (user
  call: a host who forgot it would edit the wrong map); `map=view` names
  it on purpose. `many` callers fan out (`_fan_out`: one run per map
  under `Map <name>:`, a map's ❌ doesn't stop the rest); placements take
  one map. Replies name the map on a multi-map match (`_on_map`); so do
  `!ent info` (`{map_shown}` in entity_info_format) and `!ent dump`
  (789).
- **Following** (`Match.followed_map`): each turn start records the actor
  per team and for the omniscient view (`last_actors`, saved); a board
  follows only a map the team has seen (else its first listed map), there
  where that unit stands now, else its last map (a cross-map move
  updates the record), else before any turn the team's first unit, else
  the first map. `channel_maps` (`!map use`) and `channel_views` (cameras,
  per map) are per channel display state: no undo step, kept live across
  an undo (`UNDO_IGNORED`), so a player's pans can't fill the undo window;
  the journal still records a `!map use` (`JOURNAL_IGNORED` leaves
  channel_maps in), since `map=view` and strict edits read it (790, 815).
- **Unseen and hidden maps** (step 2c): a team has seen a map when fog
  is off there, or it has a living unit (root body's team), a reveal or
  fog memory there (`Match.map_seen_by`; an omniscient view sees all).
  An unseen map shows per its `unseen` setting (map var named by
  `map_unseen_var`, `!map unseen <mode>|default`), else the
  `unseen_map_display` rule: `show` (renders, all fog), `blank_named`
  (blank board naming it), `blank_unnamed` (default; listed and counted
  as `unseen_map_unnamed_label`, naming it refused), `hidden` (not
  listed, not counted, not nameable) (`map_display`, `listed_maps`,
  `map_named_for`, `map_label`, `map_heading` "map `x` (2 of 3)"). A
  blank board is `map_blank_text` (`unseen_map_message_format`): ASCII
  renders the text alone, `render_scene` returns a scene with `blank`,
  which `SceneRenderer` draws as that text (parity). Reads under a team
  view (`_resolve_maps` when not strict, `!map <name>`, `!map use`,
  `!find map:`, completion) refuse a map the view may not name with the
  same two-sided wording as a missing one (`_map_for_view` /
  `_no_such_map`); host-only strict edits state the facts. `!map list`
  under a view lists what it may know of, "not seen yet" rows without
  size; the omniscient list names each map's unseen mode. Boards: a
  Discord board browses maps (◀ ▶, a select menu past 5 maps, the 25
  around the shown map plus ◀ ▶ past 25,
  `_BoardView`; `_board_map` keeps the browsed map until `turns_elapsed`
  changes); the GUI has a "Map:" dropdown with the same rule, shown
  whenever a match is (`1. main (8x6)`: a size only for a map the view
  has seen; `GuiApp._update_map_pick`); the CLI uses `!map <name>`
  (837-838, 841).
- **Nested maps** (step 3, 844-848): `!map attach <map> <unit>` /
  `!map detach <map>` (host) make a unit carry a map (`Map.vehicle`,
  saved; a unit may carry several; a body part can't). A map can't be
  attached to a unit standing on it or inside it, and a move
  (`move_to_map`, `check_map_nesting`) can't put a carrier inside itself;
  nesting is bounded by the owner limit `map_nesting_depth` (default 4).
  Helpers `maps_carried_by`, `outer_map_of`, `map_depth`, `maps_inside`;
  formulas `map_carrier(map)`, `inner_maps(eid)` (loopable),
  `outer_map(map)`. Inner maps are ordinary maps otherwise (lists,
  boards, unseen rules, `map=`); `!map list` says "carried by `x`" only
  when the view can see x. Boarding is composed (an action in, a hatch
  tile out; `!help map attach`). `on_map_vehicle_moved` fires from
  `fire_entity_moved` for the units on every map the moved body carries
  (`map_vehicle` bound, the inner map as `context_map`), and for the
  maps nested deeper inside those whose `nested_vehicle_moves` setting is
  on (map var named by `map_nested_vehicle_moves_var`, else the rule,
  default on; a map set off stops the chain, `_vehicle_move_maps`, 862).
  A carrier's
  death (`Match._map_vehicle_died`, after `_process_death` leaves its
  guard): per occupant in turn order the death effect (`__map_vehicle_
  death_effect` > the vehicle's var named by `map_vehicle_death_effect_var`
  > the rule) and `on_map_vehicle_death`, then the survivors are ejected
  around the vehicle's body (`_eject_spot` / `_nearest_fit_cell`, which
  skip cells that block the unit for tp; no room = stays, ⚠️), then
  `map_vehicle_death_map` (`__map_vehicle_death_map`): destroy kills the
  rest and drops the map, the corpses this death made ejected or lost
  per `map_vehicle_corpses` and those already lying there per
  `map_vehicle_existing_corpses` (both default eject, per-unit
  `__`-prefixed overrides); wreck keeps it and every corpse on it, records it in the corpse
  (`maps`, a revive re-attaches it) and runs the wreck formula
  (`map_vehicle_wreck_var` / `_formula`) with the wreck as its map. A
  despawn ejects and detaches (`_release_inner_maps`, from
  `Entity.remove`); transfers of a carrier are refused. The map var
  `__delete_when_empty` deletes a map of its own once no unit is on it
  (swept after each top-level command, `sweep_empty_maps`). A walk ends
  where a hook put the walker (`move_dirs`, `move_group_dirs`).
- **Roster on several maps:** `!list` / `!state` group units by map then
  team, or team then map (`roster_map_grouping`; teams in order of first
  turn, the team a disguise shows), corpses by map (`_roster_groups`); a
  one-map match lists as before (840).
- **Cross-map moves** (`Match.move_to_map`, `!ent tp <id> <x> <y>
  [<map>]`): the whole carried body (`carried_body`: parts, riders,
  segments resettled behind the head, located parts at their anchor
  offset) and its auras move; all or nothing (footprint and every located
  part must fit). Exit hooks on the old map, enter / stop on the new,
  `on_entity_moved` (`from_map` / `to_map`, equal for an ordinary move),
  then `on_entity_changed_map`. `!ent add` / `!ent clone` / `!template
  spawn` take `map=` (791, 793).
- **Queries:** units on different maps have no distance (`!dist`
  refuses); `!find` / `!foreach` take a `map:<name>` selector, `near:`
  matches only the reference's map, `within:` reads its cell on the
  `map:` word's map else the channel's (`_bind_find_maps`); `!foreach`
  substitutes `$map` (792). Death / revive line tags carry the map
  (`UNIT_LINE_TAG` `<match>|<unit>|<x>|<y>|<map>`).
- **Grid:** square cells, 1-based; `max_grid_dimension` (default 500, owner
  ceiling) checked on create and resize. `!map resize <w> <h> [anchor]`
  shifts everything coordinate-bearing by the anchor's offset (units, tiles,
  corpses, zones, fog memory and reveals, camera views, snake trail vars);
  a shrink that pushes units off follows `map_resize_shrink_mode` (block /
  kill, looping until nothing is off-grid).
- **Movement verbs:** `!ent tp` / `move` (count first, `allow_diagonal_movement`)
  / `push` / `pull` / `swap`, group moves (`group:<name>`,
  `mount_group_move_mode`), formula `move_entity` / `move_step` (a real
  one-step walk) / push / pull / swap. Every verb validates the WHOLE swept
  footprint and the final footprint (`_validate_placement`,
  `_occupancy_ignore` for the mover's own body / segments / group), fires
  per-cell hooks and on_entity_step, stops when the mover dies, and
  redirects a mounted driver to its vehicle (`_mount_move_redirect`; a
  passenger is refused; a glued or region part can't move by itself, the
  refusal names its root, `Match.body_root`). `!ent swap` checks the two
  new footprints against each other. The raw `move_to` and spawns are
  never gated.
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
- **Teams and relations:** a unit's team is the `team_var` var, stored and
  read as text (`team_text`, §7.1; None / "" = no team). `relation`
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
  Membership in an area = ANY covered cell. Every area search (burst,
  rect, cone, line, zone, `!find near:` / `within:`) leaves out glued
  parts and riders hidden in a vehicle (`formula._alive_at` /
  `_occupants`; 933). Every line is the sight walk (`logic.line_cells`:
  cells_in_line, `!map preview line`, `!reveal_fog line`, `!tile line`,
  has_los, the line searches; 935). Builders are clipped to the map
  and charged to `formula_cell_limit`. `!dist` and `!map preview
  <burst|cone|line|rect>` show the same geometry. Block conditions also
  see the step (`move_dir`, §7.12 "Facing").

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
  A region part is measured by its own cells: `Match.body_rects` (one
  rectangle per row run of its region) behind entity_gap_distance /
  cell_entity_distance, `entity_occupies` tests its cells, `body_center`
  (the region cell nearest its box's centre) behind entity_center /
  aoe_origin; it never is a cell's occupant (its parent is) (934).
- Movement hooks fire per covered cell (a 2×2 crossing a fire band burns once
  per fire cell). A large corpse keeps one id; its cells derive from its
  stored footprint vars. Prims: `footprint_width/height`, `footprint_cells`,
  `occupies`, `entity_center` (floored), `aoe_origin` (`aoe_origin_mode`)
  (382-386).

### 7.10 Body parts, segments and mounts

- **Parts** are real units with the protected `part_of` field; a parent's
  parts are derived by scanning (`parts`, `part`, `has_part`, `part_of`,
  `entity_part_subtree` BFS parents-first). `parent` token. Created by
  templates (`parts` key, `{role: part}` or a list; a part loop is
  refused) or `!part
  add/attach/detach/remove/glue/locate/region/segment/list/info`. `attach`
  refuses a loop, a map carrier, a snake segment, a snake head with
  segments and a rider (a corpse's kept rider too), and
  re-stamps the unit's auras, riders and parts; a loaded loop is cut and a
  map a part carries freed. A living detached part steps to the nearest
  free cell; detach and locate carry the part's own auras, glued parts and
  riders. `glue` / `region` refuse a snake segment. A group move leaves
  out a glued / region part whose body is in the group and refuses one
  whose body isn't (873).
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
  re-lays the body (`_resettle_snake`: straight behind the head where the
  cells are free and on the map, else bending toward the nearest free
  cells, a ⚠️ for a segment with nowhere to go); `segment_self_collision`.
  A walk checks the whole body's final cells: a segment that would end on
  another unit's cell refuses the move (single and group moves,
  `_check_snake_trail`; the head may still pass over units, 829).
  After the head moves, the body is laid first, then each segment
  carries its riders and anchored auras (`_restamp_segments`) and the
  parts glued to it follow (879). Segments go on the head only (a segment of a segment would branch).
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
  A vehicle's death (`Match._mount_vehicle_died`, after `_process_death`
  leaves its guard, before the map-vehicle handling): per rider in turn
  order the death effect (`__mount_death_effect` > the slot's
  `death_effect` > the vehicle's var named by `mount_death_effect_var` >
  the `mount_death_effect` rule) and `on_mount_vehicle_death` (`vehicle`,
  `slot` bound), then its fate, eject / kill / keep (`__mount_on_vehicle_
  death` > the slot's `on_vehicle_death` > the vehicle's
  `__mount_on_vehicle_death` > the `mount_on_vehicle_death` rule; an
  invalid value is skipped with a ⚠️; keep needs a corpse, else eject).
  A body part with seats (a howdah, a train car) runs the same rules
  when its creature dies (collected before the part subtree's removal,
  run after the death) and when it is destroyed as a limb (keep holds:
  the dead limb stays in the match) (867).
  A removal without death (despawn, transfer, transform eject) ejects
  with no effects (`_release_riders`). Ejection (`_eject_rider`) takes
  the cell around the vehicle's body nearest its centre (`_eject_spot`)
  and fires on_dismounted then on_entity_moved, so a rider carries its
  own riders (814, 849-851); a vehicle's riders don't block its own
  placement; dismount takes the free cell nearest the vehicle's centre
  (rings outward, ties by y then x);
  a dead rider revives
  unmounted; transform re-checks a mounted rider's slot (and a vehicle's
  riders) per `transform_rider_mismatch_mode`. Boarding legality is the GM's
  call through slot conditions — a built-in boarding range was REJECTED.
  `!mount <rider> <vehicle> <slot> | dismount | switch | list | info`; prims
  mount / dismount / switch_slot / riders / slot_riders / can_mount / ...
  (458-461, 465-466, 503-506, 682).

### 7.11 Death, corpses, revive, transform, disguise

- **Death and revive lines:** every death (any source, a destroyed body
  part too) and revive prints `death_message_format` /
  `revive_message_format` (`Match._announce`, placeholders incl. vars;
  empty = silent) through `surface_log`. The line carries an invisible tag
  (`logic.UNIT_LINE_TAG`: match, unit, cell); `CommandRegistry._run_top`
  wraps the reply context in `_UnitLineCtx`, which drops a tagged line the
  channel's view can't see (the unit, its corpse, or the cell when
  deleted) and strips the tag from the rest. Turn-start deaths read
  "⏭ The turn passes ..." with the death line pulled in before it
  (`_pull_surfaced`). A new line about one unit should be tagged the same
  way; turn-flow lines name no unit (769).
- **Death** (`_process_death`): `death_result` (corpse / delete, per-unit
  `__death_result`); a corpse is the unit's snapshot in its cell's tile data
  (`corpses.<id>`, mount links stripped; `corpses` is a reserved tile key
  no tile write sets, and every tile edit keeps it: place, fill, del,
  clear, `tile_clear`); `corpse_id_uniqueness`. A revive puts the corpse
  back when the unit can't be placed (`revive_blocked_mode`: refuse,
  default, or nearest = the free cell nearest the body that fits all of
  it, searched like a dismount, located parts keeping their offsets, the
  reply naming the cell, 828) and rebuilds the turn order once the unit
  is alive; the corpse records the groups of the body and its parts
  (`groups`) and a revive rejoins those that still exist, for the units
  it restored only (a part whose id was taken meanwhile is named in a ⚠️)
  (814, 820, 824). `kill` /
  `revive` run `default_kill_function_effects` /
  `default_revive_function_effects`; a kill whose effects already brought
  a death (and maybe a revive by a vehicle-death effect) runs no second
  death (873). Prims: has_corpse, corpse_at,
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
  disguise (484-485). Its `status` dict stands in for the real statuses
  (`Match.presented_statuses`: roster, `!ent info`, `!find status:`, and
  the tags statuses grant; none without the key), and a var-name rule
  rename moves its `vars` decoys too (`rename_unit_vars`); `!ent info`'s
  HP / Team read the decoy of the var behind them (940, 942).
- **Outcome:** `declare_winner` / `!match win`, `match_winner()`,
  `match_over()`; win conditions are composed (watchers, on_death passives),
  never configured.

### 7.12 Zones, tiles and auras

- **Tiles:** per-cell data (`!tile set/place/line/fill/copy/...`), templates
  (`!tile def` per match, `!system tile` per system, §7.16), precedence instance > template > rule for
  glyph / color / sprite / block / opaque; tile hooks (on_enter / on_exit /
  on_stop / time hooks). Formulas place a template with `tile_place(template,
  x, y, data=None)` (`!tile place`: replaces the cell's data, corpses stay,
  `data` overrides fields; mutating) and read it back with
  `tile_template_of(x, y)` (892). `!tile def rename` takes the placed tiles
  along (`Match.rename_tile_template`); deleting a used template needs
  `confirm` and detaches them (`_template` cleared, their data stays, so
  a later template of the name takes none of them over;
  `undefine_tile_template`, user call, 188).
- **Facing** (#257, 928-931): a tile's facing is its `facing` data, else
  `up`, a zone's its `facing` field, else `up`; most never set one.
  MATERIALIZED (user call: facing is a default or a value of the thing
  itself, like a unit's hp / x / y / facing): a placement copies the
  template's facing with its data, a zone made from a definition
  (`create_zone_from_def`, `setdef` when it has none) copies the
  definition's, and a later `!tile def face` / `!zone def face` reaches
  existing ones only through reapply (`Match.zone_look` never reads a
  definition's facing). `Match.checked_facing` (shared with units:
  cw / ccw, diagonals need allow_diagonal_facing) behind `!tile face` /
  `face_area`, `!tile def face`, `!system tile face`, `!zone face`, `!zone
  def face`, placement `facing=`, `!tile set ... facing` and tile_set
  (`tile_data_value`); `checked_facing_data` makes any stored tile
  `facing` a canonical direction. Reads `tile_facing(x, y, map=)`,
  `zone_facing(zone)`; writers `tile_face` / `zone_face` (mutating). Hooks
  call `tile_facing(tile_x, tile_y)` / `zone_facing(zone_name)` (a binding
  of the same name would hide the function). Looks: `glyphs.<facing>` /
  `sprites.<facing>` (tile data, template data, zone and definition
  fields; `!zone glyph|sprite <name> <v> [<facing>]`) before the plain
  glyph / sprite, own before template / definition (`_facing_glyph`,
  `_facing_sprite`); a missing facing sprite is mirrored per the
  `sprite_mirror` data (tile, template; zone data, definition data) else
  the rule (`_mirror_axes`); no facing-arrow fallback. Block conditions
  (tile / zone / corpse) bind `move_dir` and `from_x` / `from_y` for a step
  (`cell_blocks(step=)`, passed by walks, pushes, pulls, group moves; None
  for a teleport, swap or placement), so a one-way door is `move_dir !=
  tile_facing(tile_x, tile_y)`; `!map cell ... for= dir=` and
  `cell_blocked(..., dir=)` judge a step. A rotated / flipped prefab stamp
  turns each stamped tile's and zone's facing (`prefab._turn_facing`).
- **Reapply** (#258, 932): `!tile def reapply <t> [reset] [map=]`
  (`Match.reapply_tile_template`, every map by default) and `!zone def
  reapply <d> [reset]` (`reapply_zone_definition`): the template's /
  definition's data keys overwrite the tile's / zone's same keys, the
  others stay; `reset` makes the data a copy (a tile keeps its corpses
  only). A zone definition pushes its facing too (a tile's is data).
  Hooks, the other look fields and tags are live already. `!match sync
  tiles|zones|all [name] reapply` syncs then reapplies; `!system zone` has
  no reapply (a system's definitions have no zones).
- **Zones:** named cell sets (`!zone ...`; names and definition names
  take id characters, `check_zone_name`): data, hooks (boundary + per-cell
  + time), dedicated glyph / color / sprite / legend fields, `zone_shift`,
  `zone_*` prims, `zone_distance`; `create_zone` / `delete_zone` in
  formulas. entities_in_zone / cell_in_zone / in_zone / zone_has /
  zone_size answer empty for a missing zone with a ⚠️ once per command
  (`_known_zone`, user call); zone_exists is the silent test (946).
- **Zone definitions** (`docs/zones_prefabs_design.md` §1-§4; `!zone def`,
  `Match.zone_definitions`, `normalize_zone_def`, a system library): a kind
  of zone described once, the tile-template pattern (user call, for
  consistency between the two map features with definitions). A zone
  naming one (its `def` field: `!zone new [<name>] def=<d>`, `!zone
  setdef`, `zone_spawn`) reads the definition's hooks, look fields
  (`ZONE_LOOK_FIELDS`: glyph, color, sprite, legend) and tags LIVE, its own
  value winning (`Match.zone_hook_source`, `zone_look`, `zone_tags`; every
  hook-firing site, both renderers and the legend go through them); its
  data is COPIED in when the zone is made, and `block` / `opaque` read
  through to the definition's data when the zone's data lacks them
  (`zone_data_spec`, as a tile reads its template's). A zone without a
  name taken from a definition gets one minted like unit ids
  (`Match.mint_zone_name`: `poison_cloud`, `poison_cloud2`). Rename takes
  the zones along; deleting a used definition needs `confirm` (the
  warning names the zones and `!undo command`) and detaches them (`def`
  cleared, their own data stays), so a later definition of the name takes
  none of them over (user call, Z1, 904). `!zone info` shows what a zone
  takes from its definition (`from_def`) (904-906).
- **Tags** on zones and tiles (`normalize_tags`, `check_tag`): a zone's are
  its definition's plus its own (`!zone tags`, `!zone def tags`), a
  tile's its template's (a `SpecialTileTemplate.tags` field, read live,
  never copied into data) plus its own `tags` data (`!tile tags`, `!tile
  def tags`). Reads: `zone_tags`, `zone_has_tag`, `zones_with_tag(tag,
  map=)`, `zone_def_of`, `tile_tags`, `tile_has_tag`, `tiles_with_tag`, and
  `tag=` / `definition=` filters on `zones_at`, `entity_zones`,
  `zone_names` (`def` is a Python keyword), `tag=` / `def=` on `!zone
  list`. `zone_spawn(definition, cells, map=None, name=None)` (mutating)
  makes a zone of a definition (905, 907).
- **Auras:** a zone anchored to a unit (`!zone anchor <name> <eid> [radius]
  [metric]`): cells re-stamped around every footprint cell when the anchor
  moves or its footprint var changes (`_body_reshaped`, which also re-seats
  riders), without firing the aura's own enter/exit. On anchor loss
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
- **Fog** (per map: the `fog` map var, `!match fog on|off [map=]`, §7.8;
  `Match.fog_on(mp)`): a team sees
  the union of cells within each alive member's `fog_vision_radius` var
  (metric `fog_range_mode`), from every footprint cell. With `fog_los` the
  fog also needs a clear line. Memory (`!match fog memory on|off`,
  the map's `explored`, `fog_memory_mode` full / terrain). `!reveal_fog <team>
  ...` adds reveals (`all`, `at`, `rect`, `around`, `cone <x> <y> <dir>
  <len> [half_angle]`, `line <x1> <y1> <x2> <y2>`: the cells_in_*
  builders), optionally temporary (`turns=N`, keyed to whichever clock
  advances). Everything funnels through `_fog_team_sees`; renders
  memoize it for one pass (`_vision_memo`). Explored cells and reveals are
  saved as row runs `[y, x_from, x_to]` (`_cells_to_runs`): a whole-map
  reveal as one pair per cell cost every command ~0.6 s per save.
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
  `command_access` rule (`!system access`, per system). Per-player
  overrides (`!host access ... user=<player>`) live in the same dict as
  `@<user id> <key>` and win over both (`_effective_access(..., user)`)
  (773). No-op without an
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
  hidden unit read exactly like a missing one (two-sided wording: "Could
  not find a unit named `x`. Either it does not exist, or this channel
  currently does not have permission to view it."; an omniscient view
  keeps "Entity 'x' not found.", 839) and listings skip it
  (`_pov_hides`) across `!ent info` (and every unit an `!ent` subcommand
  opened to a non-host names: diff, store_entity_into_var and transform
  name a second one; a strict `map=` follows the view's naming rule for a
  non-host), `!action` (actor and targets; a corpse target and `!ent
  revive` through `_query_corpse`, 870), `!dist`, `!find`, `!foreach`, `!turn`,
  `!map center/cell`, `!part`, `!mount`, `!action`, `!passive`, `!clamp`,
  `!mod`, `!schedule`, `!tile info` (corpses inside tile data too).
  Every unit id a NON-HOST's typed command names (any root an override
  opened: `!part info`, `!status`, `!turn set`, `!mount`, `!template
  give`, ...) reads through the view in `_resolve_eid`
  (`_TYPED_UNIT_VIEW`, set by `_run` / `dispatch_no_snapshot`, None
  inside an action body; `view=False` for a caller that also takes other
  words). A HOST's command still reaches a hidden unit (user call: a host
  may reveal on purpose; `!ent dump` / `diff` show everything); with the
  `host_reveal_warning` rule on (default off) it ends with a ⚠️ naming the
  hidden units it named (`_HOST_REVEALS`). A glued part shows while the
  body it sits on shows (`_pov_hides`) (939). Whole-board reads (`!history diff`, `!log`, `!history list <kind>`, undo
  preview) are host-only while anything is hidden
  (`_whole_board_read_blocked`). `team_data_visibility` (own / all / host)
  filters `!team list/get`. `formula_text_visibility` (all / host, user
  call): under `host` a caller who is neither an administrator nor a host
  of the channel's match reads every GM formula as `(hidden)` (names,
  triggers and signatures stay): `_run` sets `logic.FORMULA_TEXT_HIDDEN`
  (`_formula_text_hidden`; the rule from the channel's match, else its
  default system; also around the `$()` resolution) and the reads print
  through `formula_text` / `_hide_formula_fields` (`_FORMULA_FIELD_KEYS`,
  `hooks` entries, `<field>_unless`): `!func`, `!help function`, passive
  lists / info, `!state`, `!status info`, `!action info`, `!schedule
  list`, `!tile info`, `!zone info`, tile / zone hook lists, `!template
  show` / `diff`, and the FORMULA_RULES (`!help rule`, `!system info` /
  `diff`, a history diff, `rule_get` in `$()`). Alias expansions and macro
  bodies are command lines, left readable (949-950).
  `inline_args_access` closes the `$()` route
  (579-580, 593). Map names a view may not know stay out of `!match info`,
  `!match fog`, the "name a map" refusals (`Match.maps_text` through the
  `COMMAND_POV` context) and the tagged map lines (855).

### 7.15 Rendering

- **ASCII** (`render_ascii` → `_render_ascii_impl`): layers zones < tiles <
  units < fog; glyphs (`glyphs.<facing>` > `glyph` > facing arrow; one
  character), colours (`color` var — a palette name or a formula with
  self = the unit, falling through when it fails or isn't a palette name,
  shared by both renderers via `entity_color` (772) — > `team_colors` /
  a palette-named team;
  ANSI only when `ctx.supports_color`, one code per run), legend
  (`map_legend_by_default`, `!map legend`, `legend=`; each thing names
  itself through a `legend` value — a unit's var, a tile's data or its
  template's, a zone's field (`!zone legend`), a disguise's key, a
  corpse's snapshot var: `false` leaves it out, a text replaces the label;
  `Match.legend_label_*`, shared by both renderers; a disguised unit's
  label comes only from the disguise; both legends are built from what
  the view actually draws, so nothing hidden, fogged or invisible to the
  POV is ever listed) (781), coordinate rulers
  (`map_coords`, `!map coords`, stacked column digits), hidden layers (`!map
  layer`, `hide=`), preview / sight marks (`preview_glyph`), viewport.
- **Viewport** (`viewport_width/height` 28, `viewport_mode` auto = only
  surfaces with `viewport_capable`): per-channel camera `Match.channel_views`
  (`!map pan/center/view`). Discord fits a 2000-char message by dropping
  rulers, then colour, then legend; one reply takes at most
  `reply_message_cap` messages, then a cut note, and one command at most
  `command_message_cap` (20) in all (a sweep, batch or macro posts a reply
  per line), then one note counting the rest (`flush_dropped`). Commands run from the
  Approve / Resume buttons use the same adapter (`_InteractionCtx` is a
  `DiscordCtxWrapper`); a pan click is acknowledged before it waits for
  the command lock.
- **Graphics:** `render_scene` (→ `_render_scene_impl`, parallel to the ASCII
  path over the same predicates) emits a declarative model; the engine never
  loads an image. Sprite keys mirror glyphs (`sprite` / `sprites.<facing>`,
  `sprite_mirror` fills missing facings, `fallback_sprite`), tile / zone /
  corpse sprites, background (per-map var, `!map background`; else
  `background_sprite` default `ground_default` and `background_mode`,
  placed on the whole grid), fog (`fog_sprite` default `fog_default`,
  `fog_opacity`), borders (per-map vars, `!map border`; else
  `show_borders`; per-tile overrides), `sprite_mode` (stretch default / single /
  tile), z-layers (`sprite_layer_*`, per-item `sprite_layer`), overlays
  (status definition / instance `sprite`, the `overlays` var), corpse look
  (`corpse_sprite(s)` > `corpse_default_sprite` > the living sprite greyed by
  `corpse_sprite_tint` / `_opacity`), team colour as an outline
  (`team_outline_width/opacity`, `team_tint_opacity`), colour-only cells
  (`tint_fill_opacity`), legend, rulers, highlights (`preview_color` /
  `preview_opacity`). `sprite_render.SceneRenderer` draws it; glyphs outside
  ASCII use a font that has them (`fonts/` folders, then system fonts). A
  placement builds only the window's part of its body plus an outline
  margin (`_draw_placement`; the stretch glyph fallback draws at most
  `_GLYPH_DRAW_MAX` px), so a render costs the window, never a body's whole
  area. Legend labels are cut at `_LEGEND_LABEL_MAX` characters, then by
  halving; the legend widens a map narrower than one column.
  `SpriteLoader` re-reads a sprite whose file appeared or changed (path +
  mtime) and keeps PNGs at most `_SPRITE_MAX_SIDE` px.
- **Surfaces:** `!map image`, `!map mode text|image` (plain `!map` posts an
  image), `!map autoupdate` boards (Discord, runtime-only), `!map scene
  [list]` (textual model), gui.py (canvas; zoom / pan local to the GUI).
- **Input editing** (gui.py and cli.py): Tab completes (`completion.py`;
  bash-style, a second Tab lists the choices), Up / Down recall this
  session's commands (GUI: only on the input's first / last line, so they
  still move through a multi-line block; an unsent draft comes back past
  the newest), Ctrl+R reverse search (GUI: a bar above the input, Enter
  takes the match for editing, Esc restores the draft, clicking away takes
  the match), and in the GUI Esc clears the input and Ctrl+L the log; the
  first / last line is a screen ROW, so Up / Down still move through a long
  wrapped line; keypad Enter runs like Enter. A line whose quote is still
  open continues on the next line not starting with `!` (a multi-line
  body; `quote_unclosed`, Discord too), joined with `\n`. History is per session (user
  call); the CLI keeps answers to choose() prompts out of it. Completion
  under a team view offers only what that view's queries show: unit ids,
  their teams, actions, statuses and mount slots of visible units.
  The CLI gets all of it from readline where the terminal has one.
  Sprites come from `sprites/` and per-server `data/<server>/sprites/`
  (server-side only — there is NO in-chat upload, deliberately).
- **Acting unit** (`active_unit_highlight`, default OFF (user call), once
  a turn has started): `Match.active_display_unit` picks the unit drawn for
  the actor (its body for a glued part or a region part without a glyph,
  its vehicle for a hidden rider; none when the view can't see it, where a
  team sees its own units, hidden riders included, as the turn line does,
  902). ASCII
  makes its shown cells bold (`1;` in the ANSI run code) or, without
  colour, adds `active_unit_line_format` under the map; the scene marks its
  placements `active` and `SceneRenderer` outlines them while
  `active_unit_image_highlight` is on, in place of the team outline: the
  team colour brightened halfway to white, or `active_unit_outline_color`
  (white) without a team colour, `active_unit_outline_width` thick, sprites
  and glyphs alike; `!map scene list` names the acting placement (895).
- **Intended ASCII / graphics differences:** corpses and overlays are
  graphics-only; a multi-tile body follows `sprite_mode`; colour-only cells
  are a translucent fill; a unit's colour is an outline around its sprite;
  the image legend lists every look in the window (ASCII only top-layer
  glyphs); layer order follows `sprite_layer_*`; the acting unit is bold
  (or named under a plain map) in ASCII and outlined in images.
  Everything else must agree
  (§2 parity pass) (530-535, 711-717).

### 7.16 Matches, history and the table

- **Matches:** `!match new/use/list/info/bind/unbind/channels/rename/delete/
  clone/fog/pause/resume/var/win/outcome/hosts`. `!match clone <new_id>`
  copies everything but bindings, cameras, pending requests and history; the
  cloner owns the copy, co-hosts and access overrides carry, and when a
  co-host clones the original owner becomes a co-host. Ids follow `ID_RE`;
  an id that becomes a file or folder name (match, template, save, server
  key) also passes `logic.is_file_id`, which refuses Windows device names
  (`CON`, `NUL`, `COM1`, ...) the bot could not write on Windows; a match
  id differing from an existing one only by case is refused
  (`_check_match_id_case`: one file on Windows / macOS).
- **Systems and rules:** a `GameSystem` holds rule overrides, tile
  templates, functions, aliases, default vars / passives / clamps. A match
  copies its system's rules into `match.rules` at creation and on every
  `refresh_match_rules` (any `!system set` / `!system access`, which also
  refreshes the matches of every system inheriting from it); per-match
  settings that must survive a refresh live in their own fields
  (`access_overrides`, `render_mode`, `map_coords`, ...; per-map
  settings are map vars, §7.8). Rules are left out of match files on
  disk and rebuilt from the system on load / restore.
- **System inheritance** (user design: live link, everything inherited,
  per-entry merge, refuse deleting a parent, multi-level, hiding allowed):
  `GameSystem.parent` names the system it inherits from; its `settings` and
  libraries hold only its OWN layer. `MatchManager.system_chain` (root
  first), `system_value(s, key)` (nearest own value up the chain, else the
  registry default; the MERGE_RULES — command_access, default_entity_vars,
  event_log_format by key, default_clamps by path, default_entity_passives
  by id — merge every layer over the parent's instead, a root's own value
  replacing the default as before), `system_value_source`,
  `system_library(s, kind)` (each of `SYSTEM_LIBRARIES`,
  copied into a NEW match at creation as before). Editors write an
  effective value back through `system_rule_write` /
  `system_library_write`, which store only the entries that differ from the
  inherited ones plus `GameSystem.removed` for inherited entries taken
  away (an entry equal to the inherited one isn't stored, so a later parent
  edit still reaches it; a plain rule set on a child is stored even when
  equal, pinning it — user call: a child system is a list of changes to
  its parent, so a rule the GM set deliberately stays set, while partial
  templates aim at consistency with their base). Commands: `!system new <name> [parent=]`, `!system
  parent <name> <sys|none>` (none writes the inherited values into its own
  layer; a loop and a chain past the owner limit `system_chain_depth`
  (`logic.OWNER_LIMITS`, set with `!owner ceiling`, default 8) are refused),
  `!system unset <name> <rule|rule:entry|library:name>`, `!system info`
  grouping each rule under its source, `!system list` naming parents.
  Deleting a system others inherit from is refused. A var-name rule renamed
  on a parent moves the units of every inheriting system's matches and
  renames their clamps / default vars (`_inheritors`); a child's own engine
  formula equal to its parent's after a rename is dropped (753-756).
- **System libraries** (`SYSTEM_LIBRARIES`: aliases, tile templates,
  formula functions, status definitions, tables, macros, zone
  definitions):
  `!system alias|tile|func|status|table|macro|zone <action> <system> ...` edit
  them with the grammar of `!alias` / `!tile def` / `!func` / `!status` /
  `!table` / `!macro` (action first, like alias), through the effective
  library (editing an inherited entry makes it the child's own whole
  entry; deleting one hides it). `!system status` / `!system zone` run the
  definition subcommands of `!status` / `!zone def` (`_status_def_sub` /
  `_zone_def_sub`, shared with the match command) on a scratch match
  holding the system's effective library (`_system_scratch`) and write it
  back. They reach NEW matches only
  (copied at creation, user call; a system macro has no author, so it runs
  as a host's: promoting a macro a player wrote needs `trust`, after a
  refusal naming the author and showing the body, 941); `!match sync <aliases|tiles|funcs|statuses|tables|macros|
  zones> [name] | all` copies the system's effective entries over a live match's
  (match-only entries kept, an undo step; a synced status definition
  re-caps live instances like `!status maxlevel`), and `!system <lib>
  promote <system> <name>` copies one from the channel's match into the
  system.
  Formulas a system stores (rules, tile hooks / block / opaque, function
  bodies, status ticks / hooks) go through `_system_formula_warning`: a
  disallowed construct is refused, a call to an unknown function is
  stored with a ⚠️ (a match may define it later; a promote counts only the
  system's library) (757-759, 896-899).
- **System tooling:** `!system rename` (`MatchManager.rename_system`: the
  matches on it, children's parent links, server / channel defaults follow;
  `system_name` survives `_rollback_match` and `_restore_snapshot` keeps
  the live one, since undo snapshots still name the old system), `!system
  describe` (`GameSystem.description`, own, not inherited), `!system diff
  <a> <b>` (effective rules and library entries, with each rule's source;
  open to players), `!system lint <name>` (admin: every formula the system
  and its matches store — formula rules, default passives, library tile
  hooks / block / opaque / functions / status ticks and hooks, and per
  match its functions,
  passives, unit actions, watchers, status ticks, tile and zone hooks and
  conditions; an unknown function at system level is a ⚠️), `!help rule
  <rule>` (description, allowed values, default, value on this channel's
  match and in the system chain with its source), `!match sync ... diff`
  (what a sync would change) (760-762).
- **Match vars:** `!match var`, `match_var_get` / `match_var_set` /
  `match_var_del`. **Groups:** `!ent group`, `group:<name>` selectors and
  group moves.
- **Attribution:** each command snapshot records who ran it
  (`Snapshot.by`) and each event-log entry its command's author
  (`logic.COMMAND_USER`, set in `_run_top`); `command_attribution` (words
  history / log / diff) picks where it shows. `!log [n] type= who= round=
  by=` filters (n ≥ 1; an unknown type is refused) (771, 822).
- **Roster text:** `entity_line_format`, `entity_info_format` (`{statuses}`,
  §7.4),
  `corpse_line_format`, `body_part_entity_line_suffix`,
  `mount_entity_line_suffix`, `show_corpses_in_entity_list`; `{placeholder}`
  templates over vars. Event log: `!log`, `event_log_*` rules, `log()`.
- **Transfers and undo:** `!ent transfer` is one undo step per match: the
  source's command step and an `(arrival from <src>)` step on the
  destination; undoing either changes only its own match, and the
  transfer and both undos say so (`_TRANSFER_UNDO_NOTE`,
  `_is_transfer_label`) (824). `!ent copy|transfer ... map=<map>` names
  the destination's map (`MatchManager.copy_entity(dest_map=)`); an id
  the destination can't create (a legacy id) is remapped like a
  collision (`rider` -> `rider_2`, 874).
- **Links and transfer_entity** (step 2d): `transfer_entity(eid, match,
  x=None, y=None, map=None)` (mutating; a coordinate works as x) moves a
  unit with its whole body into another match of the server, but only one
  that accepts the sender: a host of the DESTINATION runs `!match link
  <sender>` (`Match.links`, saved; `!match unlink`, `!match links`, which
  players may read; deleting a match removes it from every list, and an
  undo can't bring back a link to a deleted match), so a
  player's move onto a portal tile can send units without a host, and one
  table can't push units into another's match. The destination records
  its own `transfer_entity <eid> (arrival from <src>)` undo step; the
  sender's command step gets ` [transfer_entity]` (`MATCHES_SENT_UNITS`),
  so undoing either says it changes one side only. Inside an action the
  other matches are saved first (`logic.CROSS_MATCH_GUARD`, set by
  `run_action`), so a failed action restores the destination too. The
  command journals as state (`logic.JOURNAL_AS_STATE`); `!match link` and
  `!match delete` are journal state commands too (842-843). Each match has
  a `uid` (new for a clone, kept by a restore); a link stores the uid it
  was made to (`link_stamps`, `MatchManager.link_trusts`), so an undo or a
  replay can't extend trust to a later match under a freed id; `!match
  links` marks a stale link (854).
- **Saves files** (`!store save`, `!history export`, `!match journal
  export`) refuse to replace an existing file unless `overwrite` is given
  (`_check_overwrite`; `overwrite` in the file name's place is refused)
  (824).
- **Whole-server saves:** `!store save/load <name>` (admin, saves folder;
  load is all-or-nothing); `!history export/import` (a snapshot from another
  match restores only the board: the match keeps its id, name, hosts and
  system, and the units take its var names).
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
  turn and "other changes" (a secret rule's value is `(hidden)` to
  non-hosts, `_diff_rule_text`). The RNG's seed and position are saved with
  the match but aren't undo state: an undo keeps rolling from the live
  position. A rollback (`!dry`, `!batch strict`, a failed action) rewinds
  the history to a mark taken before it (`MatchHistory.mark` / `rewind`):
  autosaves of turns that never happened go, autosaves pruned meanwhile
  come back (816). `!alias`, `!table` and `!turn set` are undo steps.
- **Team data:** `!team set/get/add/list/clear/passive`, `team_get/set/add`
  (`team_add` refuses a non-number; `!team clear <team> [path]` takes a
  dotted path and refuses a team with nothing to clear);
  a `modifiers` bundle applies to every member.
- **Dice and tables:** `roll("NdM±k")` with explode `!` and keep `kh<n>` /
  `kl<n>` (n ≥ 1); `!roll`, `!roll odds` (exact distributions); `!roll
  none, none, crit` (a comma list picks one item, `item:weight` weighted,
  through `roll_table_pick`) and a leading `<n>x` repeating any roll, at
  most `roll_repeat_limit` (owner ceiling) (888); `roll_table`
  (finite weights, keyed entries), `band` (ranges may be negative; an
  unreadable or backwards band is refused), named tables (`!table`, `table_roll`); `pick` / `shuffle`. All use
  `Match.formula_rng()`: a `logic.CountingRandom` seeded with the
  `random_seed` rule when set, else the match's own `rng_seed` (made at
  creation, fresh for a clone), counting the 32-bit words drawn
  (`rng_pos`, saved, so a reload resumes the sequence). The sequence runs
  in epochs of `RNG_EPOCH` words (epoch k re-seeds with `<seed>#<k>`), so
  `advance` reaches any position by skipping at most one epoch. `!match
  rng [set <seed> [<pos>]]` (seed `auto` = a fresh one; host only: the
  seed predicts every roll; not an undo step, and `!dry` refuses it);
  `!dry` restores the RNG it used (777, 804). `random_seed` is a SECRET
  rule (`SECRET_RULES`): its value is hidden from players in `!help rule`,
  `!system info` / `diff` and `rule_get` inside `$()` (805).
- **Server-wide undo and audit:** every typed command that changes
  `MatchManager.server_state()` (every system and the default-system
  pointers) is one step on the server's stack (`system_history`, the state
  before it plus each match's system then, owner limit
  `system_undo_steps`); `!system history [n]` / `!system undo [n]
  [preview|confirm]` (admin) put every system back and re-apply rules to
  every match, so units move back to the var names those rules give
  (refused whole under var_name_collision refuse, or while a match uses a
  system that didn't exist then; a match follows a system rename back by
  its recorded system). `!store load` / `!server wipe` replace the server
  and clear the stack. `!server audit [n]` (admin, owner limit
  `audit_log_entries`) lists who changed what server-wide: each such
  command's system diff (`_server_state_diff`) plus its `audit_note`s
  (files, templates, deleted matches) (774-776).
- **Command journal** (`journal.py`, rule `command_journal_limit`, -1 =
  the owner's ceiling, 0 = off): per match, segments of a starting state
  (match state, its system chain, the server's templates) plus entries:
  `cmd` (a command that changed the match per the undo test, as typed —
  `$()` unevaluated — with author, channel, RNG [seed, before, after] and
  the state hash after it, watchers included; the reads in
  `_JOURNAL_READS` skip the journal's before-check; an `!approve`d / `!again`
  command is recorded as the command that ran, not the wrapper, via
  `_JOURNALED_IN`; interactive choice answers become `answer=` tokens; the
  author's admin / owner flags as `auth`, and a replay runs the entry with
  the lower of those and the replayer's; the prefab library reads it
  made as `prefabs`: `logic.prefab_read` / `prefab_read_names` record
  into `PREFAB_READS`, which `_run` keeps set through the watcher poll,
  each prefab stored once per segment and then by hash,
  `journal.prefab_reads`; a replay reads them through `PREFAB_REPLAY`,
  947),
  `state` (undo, history restore, `!run`, `!ent transfer`, `!match resume`,
  a command one of whose batch / macro / foreach lines or action `cmd()`
  lines is one of those (`_JOURNAL_AS_STATE`, set in
  `dispatch_no_snapshot`), a command that read one prefab twice and
  found it changed between, or a change made from a channel showing
  another match, found from the
  storage commit's `last_changed`), `systems` (the chain after a
  server-wide edit, when it differs from the last recorded), `template`
  (every library save / rename / delete). A journal starts before a
  match's first command (`_journal_before`) or when the match is created;
  a full segment starts the next from the state then and the one before is
  dropped. `!match journal [n]` (host only), `!match journal export
  <file>` (admin) writes the bundle; `!match replay <file> [upto=N]
  [as=<id>] [verbose]` (admin) builds the systems (replacing same-named
  ones no match here uses), templates and match, then runs each entry as
  a top-level command (`_ReplayCtx`: the author's identity and channel, no
  prompts, replies collected, a fresh partial cache per entry) with the
  RNG restored, and reports the first
  entry whose state hash differs; the rebuilt match takes the recording as
  its journal (`_REPLAYING`) (778-780).
- **Formula reference:** `formula.function_reference()` (built once from a
  throwaway match's namespace) gives every function's signature (the call
  forms its docstring opens with, `_doc_call_forms`, else the code's
  parameters), docstring and kind (reads / changes the match, pure; loopable; allowed in `$()`).
  `!help function <name>` shows one (a close-name suggestion when unknown;
  a match `!func` shows its parameters and body), `!help functions
  [reads|changes|pure|loopable]` lists them, `!help find` searches them, and
  Tab completes the names (`function` slot) (889, 901).
- **Crash log** (`logic.CrashLog`, `Workspaces.crashes`, bot-wide, saved in
  `data/errors.json` by `Storage.attach_crash_log`; memory only with match
  persistence off): an exception a command handler didn't expect, a passive,
  tile / zone hook or status tick that CRASHED, and a failed disk save each
  record an entry (`crash_ref` / `CrashLog.record`: time, server, match,
  user, the command as typed from the `CRASH_CONTEXT` `_run_top` sets, the
  traceback with file names only, absolute paths in the message cut to
  `…/<last part>`) and the reply names it (`💥 Unexpected
  error (ref N)`, `CRASHED (ref N)`). Refs count bot-wide past the owner
  limit `error_log_entries` (200); one server keeps at most
  `error_log_server_entries` (50) of them; a disk that keeps failing is
  one entry counting its repeats until a save works
  (`mgr._save_failure_ref`); a bad `next_ref` counts on from the highest
  kept ref. `!owner errors [n] | show <ref> | clear`
  (every server), `!server errors [n] | show <ref>` (admin, own server). `!as
  crash [text]` (CLI / harness) raises one on purpose (890).
- **Other:** `!help find <words>` (commands, subcommands, rules and formula functions),
  `!whoami`, `!log` (event log rules), `!state`, `!list`, `!dist`, string
  helpers (`upper` ... `fmt`, a safe field parser), list helpers (`sum`,
  `sorted`, `any`, `all`, `count`), `lowest_var` / `highest_var`.

### 7.17 Entity templates

- **What:** a server library (`MatchManager.templates`,
  `data/<server>/templates/<name>.json`) of units saved without identity
  (`Match.capture_template`): name, vars incl. current hp, statuses,
  passives, clamps, facing, the part subtree, the var names it was saved
  with (`var_names`: a spawn under other names renames). No id / x / y / links; part
  ids, `part_of` and `__follows` relative to `TEMPLATE_ROOT` (`@root_head`);
  part positions and snake trail vars as anchor offsets. `check_template`
  validates every write and load (vitals, reserved names, facing, status
  numbers, part links, passives with a hook `when` and a `formula`, clamps;
  a passive's id / a clamp's path default to its key); a summon-style
  `{role: part}` dict is accepted, each a direct part named
  `<unit>_<role>` at its x / y offset like a listed one.
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
  BEFORE moving anything; a forced partial rename / delete that rewrites
  several dependents reserves room for all of them first,
  `_reserve_template_room`, so it is refused whole or done whole).
  `!server wipe all` trashes templates.
- **Raw JSON tail:** `split_command_line` / `split_command_args` keep
  `template import <name> <rest>` whole (```json fence or single quotes
  dropped) on every surface; a multi-line import is one command on Discord,
  in the GUI (non-`!` lines after a `!template import` line join it, blank
  lines skipped) and in the CLI (`_read_continuation`: `...` lines until
  the JSON's brackets or ``` fence close, or a blank line; recalled as one
  history line).
  The same splitter runs on every line inside `!batch` / `!foreach` (cut at
  a standalone `;` outside quotes, `_split_semicolons`), macro run lines,
  action `cmd()` and alias expansions, so an import's JSON survives them; a
  `!run` file continues an import over the next lines while its JSON is open
  (`import_unfinished`, shared with the CLI). Discord groups a message's
  lines like the GUI (`discord_commands._command_lines`), so an import
  anywhere in a pasted batch keeps its JSON lines.
- **Formulas:** `summon_template` / `summon_template_near` (mutating),
  `has_template` / `template_names` / `template_data(name, path='')`
  (read-only; the template resolved through `Match.expand_template`, the
  same expansion `show ... expanded` prints), via `CURRENT_WORKSPACE`
  (718-726, 764).
- **Tooling:** `!template list [kind=] [by=] [prefix]`; `!template diff
  <name> <other | unit=<id>>` (leaf paths of the expanded forms, parts
  compared by their relative id; the unit form is host-only, as it shows
  all its vars); `!template spawn ... count=N` (N copies on the free cells
  nearest the target that fit their bodies, within `near=` if given, all or
  nothing: `_spawn_many`) (763-764).

### 7.18 Partial templates

Reusable var subtrees (an `axe_common` item, an `orc_base` statblock) in the
same library and namespace as entity templates: `{"kind": "partial",
"data": {...}}`; same permissions and version trash (727-734).
- **Two forms** (documented at `TEMPLATE_MARKER` in logic.py). FILE form, in
  templates: `{"__template": "axe_common", "damage": 8, "__removed":
  ["flavor"]}`: the partial's name, the changed values as plain keys (nested
  plain dicts merge key by key, into a missing key too; anything else,
  lists included, replaces) and deleted paths as tombstones. Removals apply
  FIRST, so a path both removed and set is replaced whole (how a dict the
  unit wrote itself is saved). LIVE form, on units: the full data plus
  `"__template": {"name", "overrides", "removed"}`. A partial may reference
  another (`axe_common` extends `weapon_base`); an entity template's `vars`
  may (`vars.__template` = whole-unit inheritance), and so may its parts'
  vars. Refs must name partials; self-references and cycles are refused at
  save / import (`check_template_refs`); resolution charges
  `partial_template_depth_limit` and `partial_template_size_limit` (a node
  budget per materialization, owner ceiling: nested references multiply).
- **Inherited containers:** a container that comes WITH a partial (orc_base's
  axe) has `"inherited": true` and starts with empty lists; its base is what
  the enclosing partial holds there (`Match._container_base`), so the
  partial's own values (orc_base setting the axe's damage) are base, and an
  edit of orc_base reaches the unit on resync / save. One copied out of its
  context (var_copy, looting, a detached parent) is re-measured by value
  against its own partial (`rebase_written` in write_var, `_rebase`).
- **Materialized, never linked:** spawn / summon / apply_statblock turn file
  refs into live data (`Match.template_materialize`; `resolve_partial`
  cached per command in `PARTIAL_CACHE`, keyed by server, match, pinning and
  name; any template command and any undo clears it). Library edits reach a
  unit only through `!template resync` (or a save and respawn).
- **Tracking** (`Match.track_template_change`, called by write_var /
  remove_var): the path goes into the NEAREST marker above it (a delete
  into `removed`); a value equal to the container's base at that moment
  takes it off ("back to the norm"), as does restoring a removed key; a
  listed parent leaves only when its whole subtree matches; a container the
  base lacks that a delete leaves behind is recorded. Engine writes through
  write_var (hp, ATB) count; engine-kept vars set directly
  (`_ENGINE_STATE_VARS`: snake links, located / region parts, the
  destroyed latch) are written by every save and kept by a whole-vars
  resync, as are vitals and default vars. `__template` / `__removed` are
  reserved var names; values bound for vars can't hold a FILE-form ref or a
  malformed marker (`check_live_markers`).
- **Saving** (`capture_template` / `capture_partial` → `template_file_form`
  → `_dematerialize`): from the markers alone, so a save means the same
  whatever the library later holds: the recorded paths with the current
  values, the recorded removals, and each inherited container's own
  deviations as plain keys; a container whose partial is gone is written in
  full without the ref, with a ⚠️ note. Storing a template (save / import /
  copy / restore / set) expands it under the size limit and checks the
  expanded result (a reserved var reached through a partial is refused
  then, not at spawn); an entity template can't replace a partial other
  templates reference.
- **Commands:** `!template save <unit> [name] path=<var.path>` (`.` = all
  vars) saves a partial; `give <partial> <unit> <path>`; `show ... expanded`
  (resolved against the library, pins ignored, nothing pinned); `resync
  <unit> [path]` (all or nothing, tracking off while it writes; an
  inherited container rebuilds from its enclosing partial; no path = the
  vars root or each outermost marked container); `detach <unit> [path]`
  (plain data, recorded in the enclosing marker so a save keeps it);
  `refs <unit>` (host-only like `!ent dump`). `delete` / `rename` of a
  referenced partial need `force` and an administrator: rename rewrites
  the refs in dependent templates (live markers keep the old name until
  resynced or saved; a rename that would close a cycle is refused before
  anything moves); delete writes the partial's own layer into each
  dependent (keeping a ref it holds itself), or the resolved data when the
  two layers don't combine. Formulas: `give_template(eid, path, name)`
  (mutating), `template_of(eid, path='')` (read-only); `var_keys` hides `__`
  keys, `!ent dump` shows them.
- **Pinning** (`template_pinning`, default off): `Match.template_pins`
  (`{name: {data, turn}}`, serialized, in undo snapshots, kept by `!match
  clone`; a transferred unit resolves against its new match) —
  `partial_data` pins a partial on its first use while the rule is on;
  off = pins kept, unused. `!match templates [refresh [name]]` lists them
  (with whether the library differs) or drops them; a save in a pinning
  match notes refs whose library copy differs, and writes a container whose
  partial is only pinned (gone from the library) in full, with a ⚠️
  (`_SAVING_TO_LIBRARY`: the library can't hold a reference to it).

### 7.19 Prefabs

A saved rectangle of one map stamped into a match (`prefab.py`; the agreed
design is `docs/zones_prefabs_design.md` §5-§9) (908-915).
- **Shape** (documented at the top of prefab.py): size, the system it was
  saved under, `definitions_all`, the var names of the footprint and team
  rules, tile data, corpses, zones (cells relative, an aura's `anchor` a
  saved id), units (entity dicts at 0-based offsets, parts and riders as
  their own entries, links by saved ids, vars in the partial-template FILE
  form), groups and the `definitions` it carries.
- **Saving** (`capture_prefab`, `!prefab save <name> <x1> <y1> <x2> <y2>
  [map=] [only=units,tiles,zones,corpses] [definitions=all]`, a host of
  the channel's match or an admin): a body (parts, riders, segments) is
  saved whole or left out with a ⚠️; a unit carrying a map is left out
  (unsupported); a zone is clipped to the rectangle; an aura goes only
  with its anchor; a corpse holding a wrecked map loses the map.
  Definitions (`_collect_definitions`): the tile templates, zone and status
  definitions and `!func`s (called anywhere in the saved strings) its
  contents use that the system lacks or holds differently; all of them
  with `definitions=all`. Statuses count as used when a unit holds one, a
  saved string quotes its name (a lava tile's `status_apply(self,
  'burn', ...)`) or a carried status's removes / blocked_by names it,
  repeated until nothing new turns up (944). `check_prefab` (import and
  load) reads corpse bodies and parts as units, refuses part_of /
  mounted_on / `__follows` loops and checks carried status definitions
  (stack, tick_when, max_level, hooks, tag filters) and zone definitions
  (943).
- **Stamping** (`stamp_prefab`, `!prefab stamp <name> <x> <y> [map=]
  [mode=] [rotate=] [flip=] [team=] [confirm]`, a host command): refused
  unless the match's system is the prefab's or inherits from it
  (`system_allows`; `definitions=all` goes anywhere); the turned rectangle
  must fit the map. `mode=` (default rule `prefab_stamp_mode`): overlay /
  keep (prefab tiles win / existing tiles win; a unit where another stands
  refuses the whole stamp naming the blockers' roots) or replace (clears
  the rectangle: units covering a cell despawned whole, tile data, corpses,
  zone cells; the first run lists what goes and raises `NeedsConfirm`).
  All or nothing (`_rollback_match`). Missing definitions are added; one
  the match holds differently stays the match's, with a ⚠️.
- **Turning** (`Turn`): flip first, then clockwise quarter turns; cells,
  rectangles, facings (two steps per 90°), footprint W / H swap, snake
  trail vars. GM data keeps its directional words.
- **Ids:** a saved id is kept while the match has it free, else minted
  from it without trailing digits (`orc3` -> `orc`, `orc2`); a part named
  `<root>_<x>` follows its root's new id; ids a replace removes are free.
  `part_of`, `mounted_on`, `__follows` and aura anchors are remapped; GM
  vars holding ids stay as written. A zone name clash mints a new name
  (`mint_zone_name`, named in the reply); groups join existing ones;
  `team=` sets every stamped unit's team.
- **Spawning** (`_spawn_units`): parents and vehicles first through
  `Entity.spawn` (on_entity_spawned fires; no tile enter / stop hooks);
  riders spawn mounted, their vehicle ignored for occupancy through the
  context var `logic.PLACEMENT_IGNORE`; the saved facing is set after the
  spawn (the spawn-facing rule would replace it). Then a `prefab_stamped`
  event (prefab, x, y, map, units, zones).
- **Journal:** journals carry no prefab library; a command reading it
  (a stamp, `!map new from=`, `prefab_names`, `prefab_size`) is a `cmd`
  entry carrying what it read (§7.16, 947).
- **Library** (`MatchManager.prefabs`, `templates/prefabs/<name>.json`,
  its own namespace and trash; `storage.library_dir`, the template file
  helpers' `lib=`): `show <name> [text|image]` (rendered through a scratch
  match, the channel match's render mode by default, plus a contents
  summary), `list`, `import <name> <json>`, `rename`, `copy`, `delete`,
  `trash`, `restore`; edits need the saver or an admin, reads are open.
  Size limits cut old prefab versions with old template versions;
  `!server storage` lists both. Renaming or deleting a partial names the
  prefabs whose units reference it.
- **Elsewhere:** `!map new <name> from=<prefab> [rotate=] [flip=] [team=]`
  makes a map the turned prefab's size and stamps it; `!dry !prefab stamp`
  previews a stamp. Formulas: `stamp_prefab(name, x, y, map=, mode=,
  rotate=, flip=, team=, confirm=False)` (mutating, returns the new
  top-level ids), `prefab_names()` (loopable), `prefab_size(name)`.

### 7.20 Tags

The design is `docs/tags_design.md` (built, PRs 1-3) (919-927).
- **Filters** (`logic.check_tag_filter`, `tag_filter_matches`): a list
  (or comma / space text) means ANY of its tags; text holding `and` /
  `or` / `not` or a parenthesis is an expression (`not` > `and` > `or`,
  parsed once and cached, `_parse_tag_expression`; `and` / `or` chains
  are one flat node, nesting at most `TAG_EXPRESSION_DEPTH` (32) deep and
  `TAG_EXPRESSION_TOKENS` (256) words, 938). `check_tag` refuses
  those three words, parentheses, brackets and double quotes as tags. Pure `tags_match(tags,
  filter)`. EVERY tag field reads one: zone / tile reads and `tag=`
  filters, `!zone list tag=`, the status tag reads, status `tag:` tokens
  (`tag:(fire and magic)`, a bare `tag:fire` one tag; `!status
  removes/blockedby` split tokens outside parentheses,
  `_split_status_tokens`), the #234 restriction lists, modifier and
  shield `tags` (`Match._record_tags_ok`: no tags = always; was "all of"
  before PR 2, user call; `not_tags` stays "none of"; a modifier whose
  filter doesn't parse is left out and `!mod show` names it).
- **Containers** (§4): a var container's own `tags` are the item's;
  `inner_tags` reach every container below (the vars root's too, never
  the container itself); `exclude_inherited_tags` (`all` / `*` or a
  list) cuts what arrives from above, for that container and below
  (`Match.container_tag_sources`: own, then nearest first). Not action
  tags, not the holder's (only `grants_unit_tags` reaches the holder).
  Reads `container_tags(eid, path)`, `container_has_tag`,
  `containers_with_tag(eid, filter, root=None)` (paths, action dicts and
  `__` keys skipped).
- **Teams and maps** (§5): team data `tags` (the team's own, engine
  words allowed; `grants_unit_tags` is what members carry), checked in
  `team_set`; `team_tags`, `team_has_tag`. A map's tags are the map var
  named by `map_tags_var` (a MAP_VAR_NAME_RULES setting, checked by
  `map_setting_value`); `map_tags(map=None)`, `map_has_tag`,
  `maps_with_tag`; units don't take them.
- **Unit tags** (`Match.unit_tag_sources` / `unit_tags`, `(tag, source)`
  each once): own (the var named by `tags_var`, a VAR_NAME_RULES entry,
  stored as a checked list), then `grants_unit_tags` of the unit's
  statuses (`status_field`: an instance key replaces the definition's),
  of containers under the `unit_tag_sources` roots (default `equipped`,
  per-unit `__unit_tag_sources` / `_add`), of its team's data, then a
  part's root's own and granted tags, then the ENGINE tags derived live
  (`logic.ENGINE_TAGS`: part, segment, rider, vehicle, map_carrier;
  `corpse` on corpses). Writes refuse engine tags and malformed words in
  the tags var and every `grants_unit_tags` (`checked_tag_vars` in
  write_var / checked_unit_vars, `team_set`, `!status grants_unit_tags`);
  reads skip what isn't usable (`_read_tags`). A disguise's `vars` can
  present decoy tags (`unit_tags(e, pov_team)`, read by `!find`).
  Corpses: `corpse_tag_sources` from the snapshot plus `corpse`.
- **Reads:** `unit_tags`, `unit_has_tag(eid, filter)`,
  `units_with_tag(filter, map=None)` (alive units, parts included),
  `corpse_tags`; `tag=` on the unit searches (`formula._TAG_ARG_FUNCS`,
  wrapped by `_with_tag`; `nearest_entity` filters before choosing);
  `!find` / `!foreach` `tag:<filter>` (an expression quoted); roster
  placeholders `{tags}` / `{tags_sourced}` (the default
  entity_info_format lists the sources).
- **Statuses:** `cannot_affect_units_with_tag` (a filter; `!status
  cannot_affect_units_with_tag`): `apply_status` refuses a unit whose tags
  match (`status_tag_block`, named by `status_apply_block_reason`); a
  forced application ignores it, like immunity.
- **Library tags** (§6): an entity template's tags are its saved unit's
  tags var (`_template_tags`, partial references resolved; the var named
  by its `var_names` else the rule); a partial's and a prefab's are a
  `tags` metadata field (engine words allowed, `checked_tag_list(...,
  engine_ok=True)` in check_template / check_prefab), never copied into
  units. `!template tags` / `!prefab tags <name> [<tag ...> | -]` (no
  words shows, `-` clears; on an entity template it writes the tags var);
  a re-save over a partial / prefab keeps its tags; `!template list` /
  `!prefab list` take `tag=<filter>` and show the tags (924-925).
- **Vocabulary** (§7): the system library `tag_vocabulary` (tag ->
  description; in SYSTEM_LIBRARIES, so inheritance, hides, `!system
  unset`, info / diff, server undo and audit come with it) is read LIVE,
  never copied into a match (absent from the create / sync / scratch
  lists). `!system tags <list|add|remove> <sys> ...` (admin; engine tags
  refused). `!help tag [<tag>]` and the `tag` / `filter` completion slots
  read the channel's system (its match's, else the channel's default).
  Rule `tag_vocabulary_mode` off / warn: under warn `_run_top` sets
  `logic.STORED_TAGS` (only when a system of the server warns) and the
  store chokepoints record the tags of each tag field a write CHANGES
  (`note_stored_tags` / `written_tag_words` over `TAG_FIELD_KEYS` and the
  `tag:` tokens of the status token keys; engine tags skipped):
  `write_var`, `team_set`, map tag writes (`!map var set`,
  `map_var_set`, `set_map_setting`), `Map.tile_set_path` /
  `zone_set_path`, `!zone tags`, tile template tags, the status / zone
  definition editors (`_status_def_sub` / `_zone_def_sub` wrap a
  before / after diff), `_store_template` / `_store_prefab`. Each records
  under the system judging it (a match's own, a scratch match's, None =
  the command's channel system); the command ends with one ⚠️ per system
  naming the undeclared ones, except in a channel with a team POV (a
  hook may have written them on a unit the view hides). Spawns, summons,
  stamps and restores copy
  stored data and record nothing. `!system lint` lists undeclared tags
  with where they are once the vocabulary is non-empty
  (`_lint_undeclared_tags`: libraries, the system's matches, its
  prefabs) (926-927).

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
- **Snake overlapping its own head:** the head passes through its own body
  by default (`segment_self_collision` off), so a segment can share the
  head's cell; a template spawn, revive or transform then puts that segment
  on the nearest free cell with a ⚠️. Whether spawns should recreate the
  overlap is open.

- **A child's hide after a parent rename:** a child system's hidden
  inherited entry (`GameSystem.removed`, any library or merge rule) is
  stored by name, so when the parent renames that entry (`!system status
  rename`, `!system table|macro rename`) the hide keeps the old name and
  hides nothing; the renamed entry shows through in the child. Known
  limitation, left as is (user: note it).
- **Corpse targets and immunity:** `corpse` / `corpse_list` action
  targets skip the immune_to_actions_* check (a corpse's statuses are a
  snapshot; a dead flyer can be looted in melee). Kept for now, a known
  topic of debate (user: write it down).
- **Stamped units ignore blocking tiles:** a prefab stamp (like every
  spawn) checks occupancy only, so a stamped unit can land on a cell whose
  tile, zone or corpse blocks it. Known issue (user: list it).

- **Large match state, per-command cost** (user: discuss later): after a
  whole-map tile fill on 500x500 every command costs ~3.5 s (three full
  serializations per command; numbers under "Undo history disk growth"
  below). Options raised: a cap on what a match can hold, cheaper undo
  snapshots (diffs / copy-on-write, reusing the post-command state for the
  commit), or leaving it deferred. The command journal has the same cost
  and goes into the same design (user call, pass 43's J2 follow-up): a
  `state` entry (undo, history restore, `!run`, transfers, ...) stores the
  whole match (~650 KB on a filled 150x150), and every segment's header
  copies the whole template library.

### Ask between tasks

Design questions the user wants raised when no other task is in flight
(not in the middle of a feature or a bugfix pass). Raise one, with its
options and a recommendation, when a task has just finished and the user
hasn't named the next.

- **T3, initiative changes mid-round** (pass 43): under
  `turnorder_change_policy: immediate` the order re-sorts at once and the
  pointer follows the acting unit, so a unit crossing the pointer gains or
  loses a turn that round. Measured: A 10 / B 5 / C 1, A acts, B acts, A's
  initiative drops to 0 -> round 1 runs A, B, C, A (A twice); A acts and
  C's rises to 20 -> round 1 runs A, B and C waits for round 2. Options:
  remember who has acted this round (each unit exactly one turn a round,
  a saved set undo restores; recommended), keep it as the documented
  meaning of `immediate`, or make `deferred` the default.

### Deferred (the user wants these tracked)

- **Per-server CPU work budget** (revisit before any public rollout; X4
  folded in here by the user; the pass 43 report's X4 measured the doubling case: a `!func`, event handler
  or action calling itself twice runs 2^depth times within its depth
  limit, `f(20)` 5.9 s; pass 45 bounded the move-hook case, a hook
  making two moves per level, with `move_hook_event_limit`; macros and nested batch / foreach runs now share
  macro_step_limit, the rest wait for this budget): the
  per-call limits multiply — 10,000 loop iterations × a 1,000-cell line on a
  500×500 map is still ~7 s of a frozen bot for every server. Preferred
  shape: a deterministic work counter charged per cell walked, loop
  iteration, macro step, summon and event, per server, owner-set. Needs
  careful decisions about compromises and abuse.
- **Undo history disk growth:** every snapshot is a full match state; if
  history becomes the bulk of a server's disk use, revisit retention defaults
  or a diff-based snapshot format. Measured (pass 40): after a whole-map
  `!tile fill` on 500x500 every command costs ~3.5 s (the match is
  serialized three times per command: undo pre / post state and the
  commit); `cell_occupant` scans every unit per cell, so cell searches cost
  cells x units.
- **Combat layers:** armor (coverage %, directional, damage-type AR vs ARP),
  the to-hit roll (accuracy / evasion / suppression, separate from
  hit_location), AP / FP / ARC action economy and REACTIONS (block / dodge /
  counter during another unit's turn — the choice-replay shape is the
  groundwork), fancier revive (regrow from a template). Interactive Discord
  menus for `choose()` (Discord relies on `answer=` tokens today).
- **Decide later (user):** configurable side NAMES for directional prims;
  per-damage-type `to_main_percent`.
- **Multiple maps per match (#40) and nested maps (#164):** designed with
  the user over a long discussion; the agreed decisions, deferred points, build
  order and test requirements (20+ scenarios) are in
  `docs/multi_map_design.md`. Read it whole before touching either; ask
  before deviating from it. Built: step 1 (the Map model), step 2a
  (management, map vars, command map resolution, following, cross-map
  moves), step 2b (formulas), step 2c (unseen / hidden maps, board
  browsing, roster grouping; §7.8), step 2d (`transfer_entity`,
  `!match link`; §7.16) and step 3 (nested maps, §7.8). The design doc's
  §11 lists what stays deferred (vision between nested maps, ...).
- **Later:** graphics mouse select / drag and
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
