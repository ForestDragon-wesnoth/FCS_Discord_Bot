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

### Backwards compat is irrelevant this early

The user has said this repeatedly. **Rewrite > dual-implementation
shim.** Don't add compat hacks. If a refactor breaks an old behavior,
update the call sites and move on. (Existing scenarios may legitimately
break — fix them, don't paper over.)

---

## 2. Process discipline (the things you'll forget)

### Always start with `git fetch origin main`

Multiple sessions burned tokens because Claude built on a stale main,
got into stacked-branch hell, and had to rebase / retarget PRs. The
**first command of every new task** should be:

```bash
git fetch origin main && git checkout main && git pull origin main && git checkout -b claude/<descriptive-name>
```

If you're continuing from a previous session, check whether prior PRs
have merged — the user merges fast.

### ONE active PR at a time

Unless the user explicitly says otherwise, only have one PR open. If
they ask for a follow-up on a feature whose PR is still open, **fold
the new work into the existing PR's branch** — don't open a second PR.
The user has corrected this twice now. The recipe:

```bash
git checkout <existing-feature-branch>
# add new changes, commit, push
# update the existing PR's title/body to cover the combined scope
# if you had started a second branch, close its PR and delete the branch
```

### Verify the PR is still open after every push

Pushing to an existing feature branch does NOT silently extend an
already-merged PR. If the user merged the PR while you were doing
follow-up work, your follow-up commits become orphaned on a dead
branch — they show up nowhere, and the user only notices when GitHub
prompts them with a "Compare & pull request" banner. **After every
`git push` to a feature branch, run `git fetch && git log
origin/main` (or check the PR's state via the MCP tool) to confirm
the PR is still open.** If the PR merged, the unmerged commits need
either a rebase or a fresh branch and a new PR — don't just keep
pushing.

### Run the regression after every meaningful change

```bash
python run_scenarios.py
```

The harness is fast. Run it after every commit-worthy change. If it
breaks, fix it before adding more code. **But the harness only
catches Python exceptions and "Syntax error" — it does NOT verify
behavior.** A scenario can "pass" with a `❌` reply that means the
opposite of what it should. Always also do at least one of:

- Run the new scenarios with `-v` and read the per-line transcript
- Write a small Python assertion script (see existing examples in
  prior commits' verification work) that parses `!ent dump` / `!list`
  output and asserts exact end-state values

### Commit messages: dense, factual, no fluff

Look at existing commit messages on `main`. They explain WHY a change
was made, name the mechanism, flag known trade-offs. They don't have
sections like "Closes #N" or emoji or marketing language. Match this
style.

### Tool-search MCP servers gracefully

`mcp__github__*` tools drop and reconnect frequently. After every
notification that they're back, use `ToolSearch` to load the schema
before calling. Cache: `mcp__github__create_pull_request`,
`mcp__github__update_pull_request` are the two you'll use most.

### Don't trust your own scenario expected-text

You'll write Expected: prose for new scenarios based on your mental
model. **Sometimes you'll be wrong** (the user has caught off-by-one
errors in round counting more than once). Run the scenarios and read
the actual output before declaring done. If the prose doesn't match
reality, update the prose (the prose is documentation; the actual
behavior is what shipped).

---

## 3. Codebase map

### Files (with role)

- **`logic.py`**: the domain model. `Entity`, `Match`,
  `MatchManager`, `Passive`, `ClampSpec`, `SpecialTileTemplate`,
  `FormulaFunction`, `GameSystem`. **`RULES_REGISTRY`** at the top is
  the single source of truth for every gamerule. **`HOOK_NAMES`** is
  the registered event surface. The vars chokepoint
  (`Entity.write_var` / `remove_var`) and status chokepoint
  (`Match._emit_status_diff`) are where almost every cross-cutting
  feature hooks in (death checks, var hooks, clamps).

- **`formula.py`**: the sandboxed expression/program
  language. The validator (`_EntityAccessTransformer` +
  `_validate_tree`) rewrites `entity[X].path` reads/writes into safe
  `__read`/`__write` calls and rejects anything outside the
  whitelist. `_MATCH_FUNC_NAMES` is the list of identifiers callable
  from formulas — add a function there AND register it in the
  namespace builder when you add a new primitive. `_LOOPABLE_FUNCS`
  gates what `for ... in <func>` accepts.

  Action bodies use the same engine with `action_mode=True`, which
  enables bare-name assignments (locals), `cmd`/`fail` calls,
  attribute access on `source`/`args`/`target` proxies, and dynamic
  `entity[<binding>]` lookup.

- **`vtt_commands.py`**: the user-facing command surface
  with `CommandRegistry`. Every `!command` is a `@registry.command`
  decorator. The dispatcher (`CommandRegistry.run`) takes the
  pre/post snapshot for undo history and resolves aliases. There's
  also `dispatch_no_snapshot` for `!batch` / `!run` / action `cmd()`.

- **`action.py`**: the action subsystem. `Action`
  dataclass, `discover_actions` (walks vars looking for `.actions.`
  subdicts), `SourceProxy` / `ArgsProxy` / `Coord` (the runtime
  bindings the body sees), `run_action` (transactional runner —
  takes pre-state, fires the body, rolls back on `fail()` or
  exception). `_BufferCtx` + `_sync_dispatch` solve the
  "synchronous formula engine needs to dispatch an async command"
  problem.

- **`match_history.py`**: snapshot storage for autosave/undo. Three
  flavors: round, turn, command. `Snapshot.state` is
  `Match.to_dict(include_history=False)`.

- **`test_sequences.txt`**: scenario integration tests.
  Each `SCENARIO N — title` block has `!command` lines and an
  `Expected:` prose section. **The prose is not machine-checked.**
  Multi-line bodies use literal `\n` (the harness pre-translates them
  via `_interpret_escapes`).

- **`run_scenarios.py`**: the harness. Catches `💥` and "Syntax
  error" in command output as failures. **Does NOT verify behavior
  correctness** — see process discipline above.

- **`discord_commands.py`** / **`bot.py`** / **`cli.py`**: the three
  surfaces that wire the registry into a real chat client (Discord),
  a REPL (CLI), or the harness. You'll rarely touch these.

### The cross-cutting chokepoints (memorize these)

| Chokepoint | What flows through | What hooks in |
|---|---|---|
| `Entity.write_var` / `remove_var` | every var mutation | clamps, var hooks (`on_var_*`), death check (top-level only) |
| `Match._emit_status_diff` | every status mutation | status hooks (`on_status_*`), death check |
| `CommandRegistry.run` | every `!command` | pre/post snapshot for undo, alias resolution, snapshot suspension during actions, summon-budget reset |
| `action.run_action` | every action invocation | pre-state capture for rollback, `_action_depth` bump, on_action_used/failed/on_target firing |
| `Match.summon_entity` | every dynamic entity creation | id uniqueness, occupancy (with `__cell_stackable` bypass), `on_entity_spawned`, summon-budget guard |
| `Match._process_death` | every death | on_death, corpse storage vs delete, turn-order rebuild |

Add cross-cutting features here, not at the call sites.

---

## 4. The formula sandbox — what you can and can't do

**Allowed AST nodes** (`_ALLOWED_NODES`): Module, Expression, Expr,
Assign, If, Pass, For, Tuple, BinOp, UnaryOp, BoolOp, IfExp, Compare,
Call, keyword, Attribute, Dict, List, Name, Constant, Load, Store,
arithmetic operators, comparison operators (including In/NotIn), And,
Or. **Notably banned**: Subscript (except `entity[X]`), Lambda, While,
Comprehensions, Import, Try, With, Class/Function defs, AugAssign,
chained/tuple Assign.

**Identifier surfaces** that resolve at runtime:
- `_ALLOWED_FUNCS` — pure-Python helpers (min, max, abs, round, ...)
- `_MATCH_FUNC_NAMES` — match-bound funcs (distance, entities_within,
  summon, kill, has_action, var_keys, ...)
- `HOOK_CONTEXT_NAMES` — bindings populated from EvalCtx.extras
  during hook fires (action_name, actor, fail_reason, target, args,
  ...)
- `_ENTITY_TOKEN_NAMES` — self / this / current
- `known_funcs` — user-defined `!func def`s on this match
- `known_params` — for-loop variables (and locals in action mode)

**`entity[X].path`** is the read/write surface for entity vars. X
can be a literal id, `self`/`this`/`current`, a known param, an
action binding (target), or any HOOK_CONTEXT_NAMES name (actor, etc.).
Other bare Names inside `entity[X]` are treated as **literal entity
ids** for backward compat — be careful.

**Action mode** lifts three restrictions:
1. Bare-name assignments (`raw = 5`) become locals
2. `source.x`, `target.x`, `args.x`, `loc.x` (any bare-Name root)
   passes through to Python runtime attribute resolution
3. `for x in target:` (loopable Name) when `target` is a list

The validator pre-collects all `Assign` target names so RHS refs
forward-validate.

**Important runtime semantics:**
- `_fire_var_events` runs hooks bottom-up for subtree writes
- `Match._var_event_depth` guards passive recursion
- `Match._action_depth` suspends per-command snapshots during actions
- `Match._death_processing` prevents recursive death checks
- `Match._summon_count` per-command summon budget

---

## 5. Specific traps and bugs that bit prior sessions

These are real bugs Claude shipped and then fixed. **Don't reintroduce
them.**

### Action `cmd()` async crash

The formula engine is synchronous; `ctx.send()` in the real Discord
bot awaits a network round-trip. Driving an async coroutine with
`.send(None)` raises `await wasn't used with future` on a real
suspension. Fix: `_BufferCtx` collects send output synchronously
during the action body; the top-level `run_action` flushes through
the real awaitable ctx after the body completes. Don't break this.

### CLI line-continuation `\n`

shlex preserves literal `\n` (two chars: backslash + n) from
CLI/Discord input. The formula sandbox can't parse a Python
line-continuation followed by `n`. Fix: `formula.normalize_body_source`
translates `\n`/`\t` at every user-input body boundary (passive add,
gpassive add, func def, action discovery via `Action.from_dict`). If
you add a new body-accepting command, **call this helper**. The
scenario harness has its own `_interpret_escapes` that does the same
thing earlier — so harness scenarios will MASK this bug. You must
test with raw shlex input, not just the harness.

### `entity[<binding>]` literal fallthrough

The `_who_arg` helper defaults bare-Name subscripts to LITERAL ids
unless explicitly handled. After adding new HOOK_CONTEXT_NAMES, also
ensure they're in `_who_arg`'s dynamic-evaluation branch — otherwise
`entity[actor].hp` reads the (nonexistent) entity with id `"actor"`.
The handler is at `formula.py: _who_arg`.

### Stale entity after damage

`Entity.damage_entity` previously assumed the entity stayed in the
match. The death pipeline detaches entities; `_require_match()` then
raises. Always guard `self._match is not None` after any mutation that
could trigger death.

### Scenario harness ≠ correctness

I'll say it again: **the harness only catches exceptions.** A
scenario that should output "Damaged foe by 5" but actually outputs
"❌ Cell occupied" will PASS the harness. Write end-state assertions
that parse `!ent dump` / `!list` output.

### Don't blindly trust agent recommendations

You may launch an Explore or general-purpose agent to survey the
codebase. The agents are useful but get things wrong — they'll claim
features don't exist when they do, miss key context, or recommend
features that already shipped. **Verify every agent claim with grep
before presenting to the user.** Past survey agents have hallucinated
~30% of their recommendations.

---

## 6. Workflow patterns that work

### Designing a new feature

1. `git fetch origin main && git checkout main && git pull && git checkout -b claude/<name>`
2. **Ask design questions before coding** if the feature has >2
   reasonable shapes. Use `AskUserQuestion` with the recommended
   option labeled. The user is decisive and appreciates being asked
   crisply, but hates being asked obvious questions.
3. Locate the existing chokepoints/primitives you'll extend. Read
   the surrounding 50-100 lines so your code matches the local
   conventions (comment density, helper naming, error message
   formatting).
4. Land the change in small commits if it's >300 lines. Each commit
   should pass the regression on its own.
5. Write scenarios that demonstrate the **happy path** AND at least
   one **failure/edge case** for each new behavior.
6. Run the harness. Spot-check verbose output. Write assertion
   scripts for hp deltas, var values, error messages.
7. Commit with a dense message naming the mechanism + trade-offs.
8. Push, open the PR, write a body that covers: summary, mechanism,
   new surface, test plan with the regression count, and a "notes"
   section flagging known limitations or follow-ups.

### Communicating with the user

- They prefer **short, direct messages** with content over ceremony.
- They like seeing the **regression count** ("330/330") and a quick
  summary of the most important behavioral change.
- They will **catch sloppy work**. Don't claim something works
  without verifying. Don't write speculative prose in scenarios — if
  the behavior is X, write that X is the expected.
- They are **OK with imperfect first attempts** if you flag what
  needs follow-up. Don't pretend you've nailed everything when
  there are open questions.
- They are **fast** — they'll merge a PR within minutes, ask for the
  next feature immediately. Sync main and start a fresh branch
  before doing the new work.

### When something feels architecturally murky

Ask one focused `AskUserQuestion` with the recommended path
labeled. The user has strong preferences and will tell you which
way to go. Better than guessing wrong and having to refactor.

### When in doubt, ask — implementation drift is much worse than questions

More questions is always better than the user later seeing the
implementation drifted from intent. The user has said this
explicitly. If a feature has ambiguity in shape, signature,
behavior, edge case handling, naming, or scope — **ask, don't
guess**. The user is fast to answer and treats good questions as a
sign of care, not slowness. They will be unhappy if they have to
ask "why does this do X instead of Y?" after merge.

Prefer `AskUserQuestion` with a recommended-option-first list over
free-text questions; they're easier to answer. Skip questions
about things that are obviously settled (don't ask "should I
keep the existing test passing?"), but otherwise the bar for
asking is low. A 30-second clarification beats a 300-line refactor.

---

## 7. Current state of the project (as of this handoff)

Shipped capabilities (roughly chronological; all merged):
- Match history / undo system
- Passives + var hooks + tile hooks
- Status system (entity.status dicts; self-describing status
  definitions — see the rich-statuses entry below)
- Clamp system (entity + system-level)
- Tile templates + tile time-hooks
- Formula functions (`!func`)
- Alias system (per-match + per-system)
- `!batch` / `!run` for grouped commands under one undo entry
- `!history diff` between snapshots
- `!find` with predicate prefixes (status:, group:, action:)
- Push/swap/step movement primitives + `on_entity_step` hook
- **Movement blocking (impassable tiles / zones) — SHIPPED.** A cell blocks
  a mover when its tile OR any covering zone evaluates a block condition
  truthy for that entity. Conditions are formula EXPRESSIONS (NOT action
  mode — read mover vars via `entity[self].flying`, not `self.flying`) so
  blocking is conditional ("short wall blocks unless flying"). Resolution
  mirrors glyph layering: a tile's own `block` data field > its template's
  `block` > the `tile_block_condition` rule; zones use the zone's `block`
  data > `zone_block_condition`. A block value is a formula string OR a bare
  bool/number (`block=true` = always impassable). Fail-OPEN: a malformed
  formula — or one reading a var the mover lacks — does NOT block (give
  gating vars a default via `!defvar` so reads resolve). Hooked into every
  movement verb (`Entity.tp`, `Entity.move_dirs` via a new `block_mode`
  param, `push_entity`/`pull_entity` prefix-walk, `swap_entities`); the raw
  `move_to` primitive and spawn/summon are never gated. Per-kind toggles:
  `block_walk`/`block_tp`/`block_push`/`block_swap` rules (all default True;
  walk/tp/swap RAISE `Blocked`, push/pull stop at the cell before the wall).
  Core helpers: `Match.cell_blocks(mover, x, y)` (raw geometry) +
  `_check_block(mover, x, y, mode)` (consults the block_<mode> rule).
- **`!map resize <w> <h> [anchor]` — SHIPPED.** `Match.resize_grid(new_w,
  new_h, anchor)` repositions ALL coordinate-bearing content (entities,
  tiles, corpses-in-tile-data, zone cells, fog `explored`) by an offset
  derived from a 9-point anchor (`_RESIZE_ANCHORS`): the compass point
  where existing content stays put. top-left (default) = offset (0,0)
  (coords unchanged, grow/cut at bottom-right); right/bottom = full delta;
  center/middle = half. Shrinking that pushes a live entity off-grid obeys
  the `map_resize_shrink_mode` rule (enum block|kill, default block): block
  RAISES listing the offenders (no change); kill runs `kill_entity` (the
  configured kill function) on them then proceeds. Off-grid tiles/corpses
  are dropped + zone cells clipped regardless (only entities trigger
  block). Host-gated via `ELEVATED_ARGS["map"]` (resize bumps the
  otherwise-`all` `!map` to host). Undoable (command snapshot).
- **Directional values / facing-relative sides — SHIPPED (first slice).** The
  facing-relative layer on top of the existing absolute geometry
  (`angle`/`direction_to` already give objective up/down/left/right). Formula
  primitives (all in `formula.py`): `relative_angle(facing, abs_angle [,signed])`
  (pure; abs bearing → entity frame, 0=front), `relative_side(facing,
  abs_angle [,sides,corner_arc])`, `side_hit(target, from_x, from_y
  [,sides,corner_arc])` (the headline combat primitive: which side of the
  target a hit FROM (x,y) lands on — reads target facing + pos, bearing
  target→source), `facing_of(eid)` (bridges the facing attribute, which is
  NOT a var and was previously unreadable from formulas), and
  `directional_get(eid, base, from_x, from_y [,default,sides,corner_arc])`
  (computes the side, reads `base.<side>`, default if missing — the
  one-call directional-armor read; equivalent to `var_get(eid, base + "." +
  side_hit(...))`). Sides: `front`/`back`/`left_side`/`right_side` (NOT
  bare left/right — those mean absolute map directions everywhere else),
  plus `front_right_side`/`back_right_side`/`back_left_side`/`front_left_side`
  when `sides=8`. 4-way = four 90° faces (exact for a square). 8-way: each
  diagonal CORNER spans `corner_arc`° (rule `directional_corner_arc`,
  default 30, per-call override), cardinal faces span `90 − arc` — so a
  square's corners are narrower targets than its faces; arc=0 collapses to
  4-way, arc=45 = equal octants. Helpers `_relative_side_name`,
  `_relative_angle`, `_facing_degrees`, `_SIDE_CARDINALS`/`_SIDE_CORNERS`.
  Malformed/missing-var fails by raising (FormulaError) like other funcs.
  Verified: directional armor/weakspot (back hit > front hit) via an action.
  FUTURE slices the user may want: LOS-aware raycast (stops at opaque
  cells), configurable side NAMES (gamerule), entity-shape > point hitboxes.
- Action system (full body language with cmd/fail/source/target/args,
  transactional rollback, target types entity/location/entity_list/
  location_list/none/corpse/corpse_list, recursion limit, allowlist,
  on_action_used/on_action_used_on_target/on_action_failed, `kill`/
  `revive`/`has_action`/`use_action`/etc.)
- Container var primitives (var_keys, var_sum, var_clear, etc.)
- Summon system (entity templates in vars/tiles; summon/summon_near/
  summon_from/entity_snapshot/remove_entity)
- round_number / turn_index match-clock primitives
- Death and corpses — SHIPPED:
  - Configurable death condition (default `hp <= 0`)
  - Per-entity override modes (additive/replace)
  - Corpse OR delete result; tile-data storage
  - on_death / on_revive hooks
  - kill() / revive() / has_corpse / corpse_at / all_corpses primitives
  - corpse / corpse_list action targets
  - !ent store_entity_into_var command (capture entity to template)
  - !list Dead: section
  - corpse_id_uniqueness rule
  - __cell_stackable per-entity flag
  - default_kill_function_effects / default_revive_function_effects
    / corpse_line_format gamerules
- Zones (named multi-cell regions): `Match.zones`, !zone command,
  zone_* formula functions, boundary + per-cell + time hooks, drifting
  via zone_shift. (See the zones commits.)
- **Host / access-control system + multi-channel binding** — the big
  structural one; read this before touching dispatch or the Discord
  adapter:
  - **Identity.** The command context now carries a user identity
    (`ctx.user_id` / `user_name`), which the engine previously lacked.
    Discord fills it from the message author; CLI + the scenario harness
    use a switchable `"cli"` stand-in flipped by `!as host|player`
    (gated behind `ctx.cli_mutable`). Read via `ctx_user(ctx)` /
    `ctx_user_name(ctx)` in vtt_commands.py — both return None-safe
    fallbacks so an identity-less surface DISABLES gating rather than
    locking out.
  - **Ownership.** `Match.owner` (the creator, set by
    `create_match(owner=...)`) + `Match.cohosts`. is_owner / is_host /
    add_cohost / remove_cohost. Owner = sole host-MANAGER; co-hosts share
    full command privileges but can't appoint. `!host add/remove/list`
    (owner-only). All persisted.
  - **Multi-channel binding.** `Match.bound_channels`
    (channel_key -> {"label"?, "pov"?}), uncapped. `!match
    bind/unbind/channels`; `match use`/`bind` keep
    `MatchManager.active_by_channel` in sync. The `label` is a free-form
    tag; the per-channel `pov` (on-demand fogged views) is detailed in
    Visibility Piece 1 below. PUSH auto-routing (the bot re-posting each
    channel's view on state change) is the parked auto-update idea, not
    built.
  - **The gate** lives in `CommandRegistry.run` →
    `_gate_decision` / `_effective_access` (vtt_commands.py). Per-command
    access level via `registry.command(access=...)`: `"all"` /
    `"host"` (DEFAULT, mutating — non-host's invocation is held for
    approval) / `"host_only"` (approve/deny themselves) / `"owner"`
    (host mgmt). A host-gated root auto-downgrades to "all" when its
    first arg is in `READ_ONLY_SUBCOMMANDS` (list/info/dump/cells/...),
    so players can inspect but not mutate. Gate is a NO-OP when there's
    no active match, no identity, or `owner is None` (legacy/open
    matches). Alias resolution runs BEFORE the gate; `dispatch_no_snapshot`
    (batch/run/action `cmd()`) is intentionally ungated since it's only
    reached from an already-approved/host context — gate stays at the
    top level only.
  - **Approval queue.** Non-host commands → `Match.add_pending_request`
    (runtime-only, not serialized). Surfaced as Discord Approve/Deny
    buttons (`_ApprovalView` in discord_commands.py, host-only,
    re-dispatches with the clicker's authority) OR text
    `!approve`/`!deny`/`!pending` (work everywhere, harness-testable).
  - **TWO access-override layers — don't conflate them** (this bit
    confused even me; comments at `!system access` in vtt_commands.py
    spell it out):
    1. The `command_access` RULE = system-wide default. It's a DICT rule,
       so `!system set` refuses it (dict/list rules each get a dedicated
       editor — `!log format`, `!gclamp`, and now `!system access`).
       Edited via `!system access <sys> set/clear/list`; flows into a
       match's `rules` snapshot at create + every `refresh_match_rules`.
    2. `Match.access_overrides` = per-match host tweak, edited via
       `!host access set/clear/list`. It lives in its OWN field, NOT in
       `rules`, SPECIFICALLY so a rule refresh (any `!system set` /
       `!system access`) does NOT wipe it. The gate checks
       access_overrides FIRST, then the rule, then defaults — so a
       per-match host decision always beats the system default. Do NOT
       "simplify" by folding access_overrides into rules; that would let
       `!system access` clobber every host's per-match lockdowns.
    The point of both: fog-of-war / invisibility matches can host-gate
    reads (`ent dump`, `find`, `map`) so players can't enumerate hidden
    entities — per match (`!host access`) or as a system default
    (`!system access`).
- **Visibility rework — PIECE 1 (entity visibility + per-channel POV).**
  The first slice of a since-completed arc — range fog, LOS, fog memory,
  and tile/zone/corpse visibility (Pieces 2–3 + LOS) all shipped below.
  - **Per-channel POV.** `bound_channels[ch]["pov"]` = a team string, or
    absent/`"omniscient"` = sees all. `Match.channel_pov(ch)` returns the
    team or None (None = omniscient = no filtering). Set via `!match bind
    pov=<team>` / `pov=omniscient`; shown in `!match channels`. POV is
    per-CHANNEL, not per-user (a player in red-channel sees red's view
    regardless of who they are).
  - **CLI preview.** `!as view <team> | omniscient | clear` sets a
    TRANSIENT `ctx.pov_override` (CLI-only, not persisted) — the POV
    analog of `!as host|player`. Orthogonal axis from identity (changing
    identity doesn't touch POV). `_view_pov(ctx,m,args)` resolves:
    `full` arg → omniscient; else ctx override; else channel binding.
  - **The visibility primitive.** Gamerule `entity_visibility_condition`
    (formula EXPRESSION, default "" = all visible). Evaluated per entity
    by `Match.entity_visible_to(eid, pov_team)` with `self`=entity +
    `pov_team` binding (added to HOOK_CONTEXT_NAMES). Truthy = visible.
    Omniscient (None pov) or empty rule short-circuits to visible;
    malformed formula → visible (don't blank the board on a GM typo).
    Engine hardcodes no "invisible"/"stealth" concept — it's all in the
    formula + entity data.
  - **Where it filters.** `render_ascii(pov_team)` filters the ENTITY
    glyph layer; `!list`/`!state`/`!map` filter the live-entity roster +
    map. (Tile / zone / corpse POV filtering followed in Piece 2.)
  - **Full reveal.** `!state full` / `!map full` / `!list full` force the
    omniscient view and are HOST-GATED via `ELEVATED_ARGS` (the inverse
    of `READ_ONLY_SUBCOMMANDS` in `_effective_access`: a `full` first-arg
    bumps an otherwise-`all` read up to `host`).
- **Visibility rework — PIECE 2 (tile / zone / corpse visibility).**
  Same pattern as Piece 1, three more rules, all default "" = visible:
  - `tile_visibility_condition` — bindings `pov_team` + `tile_x`/`tile_y`
    (inspect via `tile_get`/`tile_has`). Filters the tile glyph layer +
    `!tile list`/`!tile info` (a hidden tile reads as "no data", so a
    player can't probe for a trap).
  - `zone_visibility_condition` — bindings `pov_team` + `zone_name`.
    Filters the zone glyph layer + `!zone list`/`info`/`cells` (hidden =
    "not found").
  - `corpse_visibility_condition` — a corpse is a stored SNAPSHOT, not a
    live entity, so NO `self`/`entity[X]`. Bindings: `pov_team`,
    `corpse_team` (the dead entity's team_var at death; new
    HOOK_CONTEXT_NAME), `tile_x`/`tile_y`. Filters the Dead: section of
    `!list`/`!state`.
  - All four (incl. Piece 1's entity rule) now share
    `Match._visibility_visible(rule_key, pov_team, target=, extras=)`.
    Reminder asymmetry: a TILE stores its map glyph at the top level of
    its data dict (`!tile set <x> <y> glyph <c>`), but a ZONE stores it
    in a dedicated field set by `!zone glyph <name> <c>` (NOT `!zone
    set ... glyph`, which writes zone DATA).

Standing direction the user keeps reaffirming: **"more gamerules, fewer
hardcodes"** — almost every engine behavior should be configurable.

More shipped work (continuing the list above):
- **`default_entity_vars` gamerule (SHIPPED — fog precursor).** Dict rule
  (var-path -> default value) applied in `Entity.spawn` at the very start,
  before vital-var validation, filling only MISSING vars (so an `!ent add`
  / summon-template / revive-snapshot value always wins). A default can
  even satisfy a required var (e.g. hp). Edited via `!defvar
  add/remove/list` (the var analog of `!defpassive`/`!gclamp`; values
  coerce via `_parse_scalar` like `!ent set_var`, dotted paths nest). The
  intended home for `fog_vision_radius`.
- **Visibility rework — PIECE 3 SHIPPED (fog of war, RANGE-ONLY).**
  Per-entity vision-radius var (`fog_vision_radius`, defaulted via
  `default_entity_vars`); a team sees the UNION of cells within each
  alive member's radius (metric = `fog_range_mode` rule, default
  `square_radius`/Chebyshev; missing var = radius 0). Per-match
  `Match.fog_enabled` (its OWN field, seeded at creation from the
  `fog_enabled_by_default` rule, toggled by `!match fog on|off`, survives
  rule refresh like access_overrides, serialized). HYBRID: the engine
  auto-applies fog (`render_ascii` paints `fog_glyph` over unseen cells;
  each `*_visible_to` ANDs a `_fog_sees` cell check so
  entities/tiles/zones/corpses in fog hide across map + every listing)
  AND exposes `team_sees_cell` / `team_sees_entity` / `can_see` formula
  primitives (they ignore `fog_enabled` — raw sight queries). Omniscient
  POV (None) / `!… full` / fog-off all bypass. Vision math:
  `Match._within_vision` + `_vision_radius_of`; zone fog = "any cell
  seen". LOS (opaque tiles blocking sight) and explored memory remain
  SEPARATE future pieces, explicitly out of scope.
- **Fog MEMORY (explored terrain) — SHIPPED.** Per-match `Match.fog_memory`
  (own field; seeded at creation from `fog_memory_enabled_by_default`
  rule, default off = "resets to current vision each time"; toggled by
  `!match fog memory on|off`; survives refresh; serialized). `Match.explored`
  = per-team set of seen (x,y), accumulated by `Match._record_vision` on
  every entity move (`fire_entity_moved` + per-step `fire_entity_step`)
  and spawn, and seeded for present teams when memory is toggled on;
  cleared when toggled off; serialized as `{team: [[x,y],...]}`. Remembered
  cells stay un-fogged. The `fog_memory_mode` rule (enum, default `full`)
  controls remembered-cell CONTENT: `full` = everything incl. live
  entities ("once seen, stays visible"); `terrain` = only static features
  (tiles/zones/corpses) remembered, LIVE entities still need current
  vision. Two gates: `_fog_terrain_visible` (current OR remembered — used
  by tiles/zones/corpses + the map fog overlay) vs `_fog_entity_visible`
  (current always; remembered only when mode==full).
- **Line of sight / opacity — SHIPPED (first slice).** Sight is blocked by
  OPAQUE cells, modeled exactly like the movement `block` system but for
  vision. Rules `tile_opaque_condition` / `zone_opaque_condition` (formula
  EXPRESSIONS, `self`=the VIEWER + `tile_x`/`tile_y`), overridden per-cell by
  an `opaque` data field: instance > template > rule (zones: zone `opaque`
  data > rule). Bare bool/number allowed; fail-TRANSPARENT (a typo must not
  blind). Separate from `block` (window vs smoke). `Match.cell_opaque(viewer,
  x, y)` = the raw opacity query; `Match.has_los(viewer, x1,y1,x2,y2)` =
  the LOS walk: SUPERCOVER of the segment between tile centers (tiles = unit
  squares), GEOMETRIC integer DDA (cross-multiplied `(2n+1)` boundary
  compare, no floats) so it's SYMMETRIC; viewer's own cell + target's own
  opacity never block. The diagonal-corner case obeys the `los_corner_mode`
  rule (`permissive` default = only an X of BOTH flanking cells blocks;
  `strict` = any corner-touch; `open` = corners never block). Fog wiring:
  the `fog_los` rule (system-level, default False — NOT a per-match field,
  unlike `fog_enabled`/`fog_memory`) switches whether the auto-fog factors
  LOS; `Match._fog_team_sees` (= `_team_sees(..., los=fog_los)`) is the
  single funnel behind `_fog_terrain_visible`/`_fog_entity_visible` +
  `_record_vision`, so fog hiding/map-overlay/explored-memory all become
  LOS-aware at once. `_record_vision` now iterates each member's vision-
  radius NEIGHBOURHOOD (not the whole grid) — the radius-bounded perf fix.
  Formula prims: bare `can_see`/`team_sees_cell`/`team_sees_entity` now mean
  range AND los; `_rangeonly`/`_losonly` variants isolate each; `has_los(x1,
  y1,x2,y2[,viewer])` is the raw line query (all ignore the toggles). NOTE
  viewer-conditional opacity needs the gating var to exist (`!defvar`) or it
  fails transparent. Vision math in `logic.py` ~`team_sees_cell`..
  `_record_vision`.
- **Coord-return convention + entity-LOS — SHIPPED (LOS slice 2).** Two
  follow-ons to the LOS slice. (1) **Coords as return values.** The sandbox
  bans subscript/attr-on-call, so a returned `(x,y)` was unreadable; the
  convention is now pure extractors `coord_x(c)`/`coord_y(c)` (accept an
  (x,y) tuple/list or an action `Coord`; raise on None — check `c == None`
  first). `first_opaque(x1,y1,x2,y2[,viewer])` returns the first opaque cell
  strictly between as an (x,y) pair (read via coord_x/coord_y) or `None` if
  clear — `Match.first_opaque` over the shared `Match._line_cells` (the thin
  DDA path used by has_los, factored out). (2) **Entity-factoring LOS** as
  pure primitives (fog NEVER factors entities — unchanged). The old
  Bresenham `entities_in_line` is REPLACED by `entities_in_line_ignorelos(x1,
  y1,x2,y2)` (supercover thin-line, endpoints INCLUDED, walls ignored,
  near→far) + `entities_on_los(x1,y1,x2,y2[,viewer])` (STRICTLY BETWEEN —
  shooter+target excluded — sight-aware: cut at the first opaque cell via
  per-cell has_los; near→far). No `block_entities` flag (rejected — "a tiny
  body shouldn't block a shot over it"); the GM composes the block rule in
  the action loop (`for e in entities_on_los(...): if not entity[e].tiny:
  fail(...)`). NOTE `!ent set_var x v false` coerces only LOWERCASE
  true/false to bool (capitalized stays a string → truthy); formula BODIES
  use `True`/`False`.
  - Possible later: per-channel PUSH auto-routing (the auto-update idea
    below); corpse STATUS introspection (vars now exposed via corpse_var,
    status still not); elevation as a first-class rule; vision-result
    caching beyond the radius bound.
- **Large / multi-tile entities — SHIPPED.** An entity can occupy a W×H
  RECTANGLE of cells anchored at its TOP-LEFT cell (`entity[X].x/.y` stays
  the sole addressing convention; the footprint extends right/down). W and
  H live in entity vars named by the `footprint_width_var` /
  `footprint_height_var` rules (default vars `footprint_w`/`footprint_h`;
  absent or <1 = 1, so a plain entity is byte-for-byte unchanged). Set per
  entity (`!ent set_var dragon footprint_w 3`), via a summon template, or
  globally with `!defvar` (defaults are applied in `spawn`/`summon` BEFORE
  the footprint-aware bounds/occupancy check). There is NO `!ent add` size
  arg — footprint is "just a var." Core geometry on `Match`:
  `entity_footprint(e)`→(w,h), `entity_cells(e[,ax,ay])` (row-major, [0]=
  anchor), `entity_occupies(e,x,y)`, `cell_occupant(x,y,ignore=())` (the
  footprint-aware occupancy core behind `is_occupied`), and the single
  placement gate `_validate_placement(e,ax,ay,mode)` (bounds+occupancy+
  block, ignoring the mover's own cells). Policies the user chose (all
  hardcoded defaults, NOT gamerules — "only-anchor-matters" is too
  unintuitive to warrant a knob): distance = NEAREST footprint cell;
  boolean membership (fog/AoE/zone/LOS-line) = ANY footprint cell; outward
  VISION = UNION of every footprint cell's sight disc. Threaded through:
  movement (`tp`, `move_dirs` validates the WHOLE swept footprint each step
  so a body can't squeeze through a gap narrower than itself; final
  footprint must be unoccupied), push/pull (whole shifted body), swap
  (different-size legal iff each relocated footprint fits — anchors
  exchanged), spawn/summon (`summon_near` searches for an anchor where the
  whole footprint fits), `render_ascii` (glyph painted on every covered
  cell), resize (cut if ANY cell off-grid). Vision: `_member_sees` casts
  from each footprint cell; `_record_vision` unions per-cell neighbourhoods;
  target-side `_team_sees_entity`/`_team_has_los_entity` + `entity_visible_to`
  use ANY cell; entity-LOS `_occupants` (formula.py) registers each body
  cell (deduped, shooter/target excluded by id). Distance/AoE: nearest-cell
  gap distance in `entities_within`/`nearest_entity`; `_alive_at` membership
  by any covered cell. `side_hit`/`directional_get` measure the bearing from
  the target's true (possibly fractional) footprint CENTER (facing stays a
  single attribute; no footprint rotation, no edge-aware hit yet). Movement
  hooks fire PER CELL: `fire_footprint_tile_{exit,enter,stop}` /
  `fire_footprint_zone_{exit,enter,stop}` (boundary zone hook once per zone,
  per-cell hooks per covered cell) — a 2×2 crossing a fire band burns once
  per fire cell. Large corpses: ONE corpse identity (id-keyed, fully
  compatible with existing corpse targeting), footprint DERIVED from the
  stored entity vars (`corpse_cells`/`_corpse_footprint`); `corpse_visible_to`
  reveals if ANY cell is fog-visible; revive restores the footprint for free;
  corpses stay passable. New formula prims: `footprint_width`/`_height`,
  `footprint_cells` (loopable, coord-readable), `occupies(eid,x,y)`,
  `cell_entity(x,y)` (''=free), `entity_center(eid)` (center cell, floor for
  even), `aoe_origin(eid)` (center|anchor per the `aoe_origin_mode` rule).
  Scenarios 382–386. FUTURE the user may want: arbitrary/L-shaped
  footprints, footprint rotation on facing change, edge-aware side_hit,
  corpse occupancy as a gamerule.
- **Corpse var introspection — SHIPPED.** `corpse_var(eid, path[, default])`
  + `corpse_has(eid, path)` read a DEAD entity's frozen vars by dotted path
  (the loot / "was it carrying the key" / "raise with the same statline"
  patterns). Mirror var_get/var_has: corpse_var raises on a missing
  corpse/path unless a default is supplied; corpse_has returns bool, never
  raises. Read-only (snapshot immutable until revive). `corpse_team`
  remains a HOOK_CONTEXT binding only. Status introspection deferred.
  Scenario 387.
- **Entity-anchored auras — SHIPPED.** A zone can be bound to an entity as
  an AURA via reserved zone fields `anchor`/`anchor_radius`/`anchor_metric`.
  Its `cells` are RE-STAMPED (footprint-aware disc of `anchor_radius` around
  every footprint cell of the anchor; radius 0 = the footprint) whenever the
  anchor moves — hooked into `fire_entity_moved` (so tp/move_dirs/push/pull/
  swap all carry it), clipped to grid. Cells stay a concrete set, so all zone
  queries/hooks/glyph render work unchanged; the restamp does NOT fire the
  aura's own enter/exit hooks (same stance as zone_shift). On anchor death/
  despawn (both route through `Entity.remove`) the `anchored_zone_on_anchor_loss`
  rule decides: `delete` (default) drops the aura, `freeze` clears the binding
  and leaves a static zone. Surface: `!zone anchor <name> <eid> [radius]
  [metric]` / `!zone unanchor` (shown in `!zone info`/`list`); formula prims
  `zone_anchor`/`zone_unanchor`/`zone_anchor_of`. Anchor fields serialize
  (save + undo via `_zone_to_dict`/`_zone_from_dict`). Core: `_stamp_anchored_zone`
  / `_restamp_anchors_for` / `_release_anchored_zones` / `anchor_zone` /
  `unanchor_zone` in logic.py. Scenarios 388-389.
- **Rich statuses (self-describing status definitions) — SHIPPED.** A status's
  behavior no longer needs to live in the one global branch-on-name
  `status_tick_formula`. `Match.status_definitions` (name -> {`tick`,
  `tick_when`, `stack`, `max_level`, `data`}) defines a status ONCE; a status
  INSTANCE on an entity (`entity.status[name]`) resolves its behavior from the
  definition of the SAME name — the name IS the key, so NO `_template` tag
  (unlike tiles). `fire_status_tick(when)` now: per status, run its
  definition's `tick` at the definition's `tick_when` (default `turn_end`);
  a status with NO definition falls back to the global `status_tick_formula`
  at the global `status_tick_when` (full backward compat). DESIGN CALL:
  duration decrement + self-removal stay INSIDE the tick formula (no forced
  auto-decay) — the GM writes "hp -= 5*level; duration -= 1; remove at <=0"
  once in the def. Application/stacking is configurable: `apply_status` (+
  `status_apply` formula prim + `!status apply <eid> <name> [level]
  [duration]`) honors the def's `stack` mode, else the `status_default_stack`
  rule; modes `refresh`/`add_level`(capped by `max_level`)/`extend`/`replace`/
  `none` (first application just sets level [default 1] + duration). New
  top-level `!status` command (def/drop/tick/when/stack/maxlevel/data/list/
  info/apply) for match-level DEFINITIONS — raw per-entity instance editing
  stays on `!ent status`. Definitions serialize (save + undo). Core:
  `define_status`/`remove_status_def`/`apply_status` + the rewritten
  `fire_status_tick` in logic.py. Scenarios 390-391. DEFERRED (composable via
  `on_status_added`): cross-status interactions (burn↔freeze cancellation),
  resistance/immunity reducing applied levels, damage-buff scaling of applied
  level/duration. This was framed as the modest precursor to the bigger combat
  layers (damage pipeline, action economy, reactions) surfaced by analyzing
  the three FCS combat-system docs.
- **Pierce helper + composable penetration — SHIPPED.**
  `entities_in_line_until(x1,y1,x2,y2, max_targets[, viewer])` (formula.py)
  returns the first N alive entity ids the segment passes through, near→far,
  endpoints INCLUDED — the capped sibling of `entities_in_line_ignorelos`
  (the sandbox has no loop `break`, so a cap helper is the clean "pierce up
  to N" tool). With a `viewer` it cuts at the first opaque cell (LOS-aware);
  without one it ignores walls. Loopable. ARMOR-limited penetration (depth
  varies by what it hits) needs NO new primitive — loop `entities_on_los` /
  `entities_in_line_ignorelos` with your own `pen` accumulator gated on
  `pen > 0` (no break needed). Scenarios 392 (cap helper) / 393 (armor
  accumulator).
- **Mid-body action choices — SHIPPED (the interactive-action layer).**
  `choose(prompt, options)` → picked element; `choose_number(prompt, lo, hi)`
  → int in range. Action-mode builtins (in `_ACTION_BUILTINS`), supplied as
  `action_bindings` by the runner. EXECUTION = REPLAY (chosen over a
  generator/interpreter rewrite): the TOP-LEVEL `run_action` seeds an answer
  queue, runs the body, and when `choose()` has no answer yet it raises
  `ChoiceNeeded`; the runner rolls the attempt back, obtains one more answer,
  and re-runs the body from the top with answers replayed IN ORDER until it
  completes, then commits. Reuses the existing transactional rollback. Per
  attempt: RNG state is snapshot/restored (a roll BEFORE a choice stays
  stable) and the cmd output buffer is reset (rolled-back attempts don't leak
  echoes) — so side effects before a choice apply EXACTLY once (verified:
  var writes + cmd()). Sequential/dependent choices "just work" (the replay
  walks whichever branch prior answers chose). ANSWERING: `answer=<value>`
  invocation tokens feed choices in order (repeatable, bypass the last-wins
  args dict); if exhausted, the surface's `prompt_choice(prompt, options,
  lo, hi)` coroutine is used (cli.py implements it via `input()`; the harness
  has none → headless callers MUST pre-supply, yielding a clean "needs a
  choice" fail, NOT a hang). Reserved answer `cancel` (or empty/None
  interactive reply) aborts with full rollback; a bad interactive pick
  re-prompts, a bad pre-supplied one fails. `ChoiceNeeded` joins
  ActionFail/ActionEngineFault in `eval_program`'s unwrapped set so it
  reaches the runner; nested actions let it propagate to the top-level loop;
  Match runtime fields `_choice_answers`/`_choice_cursor` (preserved across
  rollback); `action_choice_limit` rule bounds the replay. Core in action.py
  (`ChoiceNeeded`, `_obtain_answer`, `_snapshot_rng`, the run_action replay
  loop). Scenarios 394-395. FUTURE: this same pause/resume shape is the
  groundwork for the bigger REACTION framework (block/dodge/counter during
  another unit's turn) — interactive Discord menus for choices are also not
  built yet (Discord currently relies on pre-supplied answer= tokens).
- Idea parked (Discord-only, not built): **opt-in auto-updating views** —
  `!map autoupdate` / `!state autoupdate` create a self-refreshing
  (edit-in-place) board message per channel that the bot updates on
  state-changing commands, HOST-ONLY by default, while plain `!map`/`!state`
  stay throwaway on-demand renders. Lives in the Discord adapter; the headless
  harness can't exercise it. The "push" half of per-channel POV.
- **Locational / body-part damage — SHIPPED (first slice).**
  The big locational-damage arc (scope ~ multi-tile entities). Reference the
  FCS3 combat-rules docx (head=150% / chest=100% / stomach=70% / limbs=30%
  "damage to main", directional hit-chance tables, armor coverage) and
  Helldivers 2's "% to main" model. WHAT SHIPPED:
  - **Parts are real `Entity`s** in `match.entities`, flagged attached via a
    protected `part_of=<parent_id>` entity FIELD (serialized; not a var). Parent's
    parts are DERIVED by scanning for `part_of==self` (no second structure); an
    `Entity.is_part` property (true only while the parent exists). A part's `x,y`
    MIRROR the parent's anchor, re-stamped on `fire_entity_moved` via
    `_restamp_parts_for` (same hook+pattern as entity-anchored auras).
  - **Skip surface:** attached parts are excluded from occupancy/`cell_occupant`,
    render glyph, group-move, zone membership, team vision (sees + is-seen +
    `_record_vision`), and the spatial/roster formula enums (`entities_within`/
    `nearest_entity` via `_candidates`, `entities_in_area`, `all_entities`).
    PROPERTY searches (`entities_with_status`/`_var`) still include parts. Turn
    order is free — a part has no initiative unless made independent (give it one
    → it acts on its own turn, the turret/eldritch case). `_validate_placement`
    skips occupancy for parts (keyed off the raw `part_of` field, since spawn
    validates before binding).
  - **HP-less zones = `0/0` indestructible entities** (compatible with every
    hp-assuming mechanic). `Match.is_indestructible(e)` = the `indestructible`
    var OR (is_part AND max_hp<=0).
  - **`parent` reference token** (child→parent), resolves like
    `self`/`this`/`current` via `EvalCtx` (now carries a `match` backref for it);
    wired into `_ENTITY_TOKEN_NAMES`, `_who_arg`, `RESERVED_IDS`. Parent→child is
    primitive-based: `parts(eid)` (loopable), `part(parent, name_or_id)`,
    `has_part`, `part_of(eid)`.
  - **Damage model = HD2 "% to main", default but heavily configurable.**
    `damage_part(part, amount)` → to-main dealt. Computes the to-main transfer
    EXPLICITLY from pre-hit values (incoming + part hp read BEFORE mutation),
    applies it to the parent via the NORMAL hp path FIRST (so parent clamp/hooks/
    death fire — kills HD2's uncapped Trooper), THEN floors the part's hp at 0
    (a 0/0 zone stays 0). Does NOT depend on clamp residue. Per-part vars +
    gamerule defaults (`part_to_main_percent_default` etc.): `to_main_percent`
    (doc 30/100/150), `to_main_cap` (`none`=uncapped/overflow · `max_hp`=HD2
    default · `remaining_hp` · `absolute:<n>`; `none` auto for 0/0), `vital`
    (part death kills parent), `indestructible` (auto for max_hp==0). Routing
    happens ONLY via `damage_part` — a raw `entity[part].hp -=` does not spill.
  - **Part destruction** (hp→0 by damage, non-indestructible): the part LINGERS
    attached & dead, fires `on_death` ONCE (latched by the `__part_destroyed`
    var; a heal above 0 clears it), and if `vital` runs the parent through the
    kill function. `check_death` skips parts entirely (they end only via
    damage_part / cascade). **Destroy effects** = the part's own `on_death`/
    passives.
  - **`hit_location(target, from_x, from_y[, aim, aim_weight, aim_bonus, mode,
    sides, corner_arc])`** → part id; modes uniform / weighted (per-part
    `hit_weights.<side>`, side from the shipped `side_hit`, default the
    `hit_location_mode` rule) / aimed (×`aim_weight` [rule default 3] +
    `aim_bonus`; bias without guarantee — a 0-weight side stays 0 unless
    aim_bonus lifts it). No parts / nothing exposed → returns the target itself.
    RNG via the match RNG (`_active_rng`, replay-safe with the choice system).
  - **Creation:** template-driven — `summon_entity` consumes a reserved `parts`
    key (dict `{role: part-template}` or a list), auto-spawning+linking each at
    the parent's cell. AND mid-match `!part add/attach/detach/remove/list/info`
    (`Match.create_part`/`attach_part`/`detach_part`). Config rides on plain
    `!ent set_var <part> ...` (dotted paths like `hit_weights.front=15` nest);
    only `part_of` is engine-special.
  - **Detach** → the part becomes a free entity at the parent's cell, keeping
    its state (a blown-off arm = a dead `0`-hp free entity; no corpse).
  - **Parent death** → parts are snapshotted into the corpse and removed (no
    orphaned visible limbs); `revive_corpse` re-spawns them (delivers "revive
    parent ⇒ revive parts"). `!ent dump` shows a "body parts:" section + "Body
    part of:"; `!part list` shows hp + knobs. Scenarios 396-398.
  DEFERRED TODOs (the user explicitly wants these tracked):
  - **AoE damage SPREAD between main and limbs** based on per-system factors
    (many combat systems want this; only "damage to main" for now).
  - **Independently-LOCATED parts** (own cell, not glued to parent).
  - **Part independently targetable by cell** (per game system) — needs more
    discussion.
  - Per-damage-TYPE `to_main_percent`; the **armor layer** (coverage % +
    directional, damage-type AR-vs-ARP mitigation); the **to-hit roll**
    (accuracy/evasion/suppression/spread, SEPARATE from hit-location); **AP/FP/
    ARC action economy + reactionary actions** (block/dodge → the reaction
    framework the choice-replay system seeded); fancier revive (regrow from
    template).
- **Stat / modifier system (derived effective stats) — SHIPPED (first slice).**
  A generic derived-stat layer: base stats stay plain vars (NEVER mutated);
  a modifier is a DATA record aggregated live from its source and combined on
  demand. PURE COMPOSABLE QUERY — the engine never auto-applies modifiers
  (the GM threads `apply_mods` through their own combat), same stance as
  "no hardcoded combat".
  - **Record:** `{stat, op, value, tags, not_tags, priority, condition}`.
    `value` + `condition` may be FORMULAS, eval'd with `self`=the modifier's
    owner plus the call's context entities (`target`/`attacker`/`defender`/
    `other`, each optional — added to HOOK_CONTEXT_NAMES; `target`/`actor`
    were already there). So "+25 vs undead" = `condition:"entity[target].undead"`,
    and a value can scale (`"(entity[self].max_hp - entity[self].hp)"`).
  - **Sources aggregated live** (`Match._raw_modifier_records`): every status
    instance's `modifiers`, a direct `entity.modifiers` slot, and each scan-
    root subtree (the `modifier_sources` rule, default `equipped`, walked for
    nested `modifiers`). Per-entity `__modifier_sources` (replace the default
    list) and `__modifier_sources_add` (extend it). Equip = move the item
    under a scanned root; an `inventory` copy doesn't apply. A bundle is a
    LIST of records OR a DICT of named records (the dict form is what
    `!ent set_var hero modifiers.fireboost.op add` builds); `tags`/`not_tags`
    accept a list or a CSV string — so the whole thing is command-authorable.
  - **Tag match:** required ⊆ query tags AND excluded(not_tags) ∩ query empty.
  - **Fold (`apply_modifiers`):** `eff_priority = priority + per-op offset`
    (`modifier_op_priority` rule, a CSV `op:offset` string); group by priority,
    combine same-op within a tier (add→sum, inc%→sum, more%→product, set→last,
    min→floor, max→cap), apply tiers ascending; `modifier_op_order` (CSV)
    breaks ties between different ops in a tier. Defaults reproduce
    `((base+Σadd)×(1+Σinc%))×∏(1+more%)` then set/clamp; bumping one record's
    `priority` pulls it into its own tier.
  - **Surface:** formula prims `apply_mods(entity, stat, base, tags, target=,
    attacker=, defender=, other=)` → number and `list_mods(...)` → the active
    records (introspection / `len()` checks). Read-only `!mod show <eid> <stat>
    [base] [tag ...]` renders the active modifiers + folded result (context-
    dependent ones show only when their condition resolves context-free).
    Scenarios 399-400. FUTURE the user may want: more context roles; modifier
    `source` tracking in the breakdown; per-stat caps; modifiers that
    themselves grant tags. This is groundwork the combat refactor (damage
    types, armor AR-vs-ARP, to-hit) will lean on.

For context on the latest design conversations and rationale, read the
descriptions of the most recently merged PRs on the repo (they're dense
and explain the "why").

---

## 8. Final advice

**Read this whole file before doing your first edit.** The user has
shipped 50+ PRs of work with prior Claude instances. The codebase has
patterns. Match them. Don't reinvent.

The user is genuinely a great collaborator — clear about goals,
decisive on design questions, appreciative of good work, blunt about
mistakes. Build that trust by being precise and process-disciplined.

When you finish a task, give a **short, factual summary** of what
shipped. Mention the regression count. Flag what you're uncertain
about. Move on to the next thing the user asks for.

Good luck. The system is fun to work on.
