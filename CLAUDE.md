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

### How many scenarios per PR (coverage rule of thumb)

The user enforces this — a prior session shipped many PRs with only ONE
scenario each, which under-tested complex features (e.g. modifiers without a
formula that actually CONSUMES them; multi-tile body parts without a real
composite shape). Match coverage to PR size:

- **Simple PR** (one small primitive, one rule): **1-2** scenarios.
- **Complex PR** (a subsystem, multiple sub-features, or anything with
  several interacting knobs): **3+** scenarios, ideally more — one per
  distinct behavior, plus a failure/edge case.

Each scenario should exercise a DIFFERENT facet, and at least one should
prove the feature in REAL USE (a formula/action that consumes the new
primitive end-to-end), not just that the setter command runs. When you add
follow-up tests for an older feature, insert them right after that feature's
existing scenarios and renumber the rest (the tail-rewrite pattern); update
the `Scenario N` citations in this file to match.

### ALWAYS smoke-test new features on a multi-tile entity

Multi-tile / footprint entities (and their cousins: body parts, mounts,
anchored auras, segments) are the SINGLE biggest source of interaction bugs
in this codebase — almost every audit-pass fix traced back to code that
silently assumed a 1×1, anchor-only entity (anchor-only bounds/occupancy,
LOS/vision cast from the anchor cell, a hook or clock that skipped attached
parts, a carry/restamp that moved the anchor but not the footprint). So:

**Any new feature MUST be exercised at least once against a multi-tile
entity (give something a `footprint_w`/`footprint_h` > 1, or a body part /
rider / segment) before you call it done — even if that check never becomes
a committed scenario.** A throwaway Python repro or a `-v` transcript is
fine; the point is to actually run the new code path with a footprint and
confirm it uses the WHOLE footprint, not just `(x, y)`. Ask the standard
questions: does it measure/membership-test by ANY covered cell? does it
validate the WHOLE swept footprint? does it carry the whole body on move?
does an attached part get included where it should (and excluded where it
shouldn't)? If the feature is spatial, vision-related, movement-related, or
fires per-entity, this is non-negotiable.

### ANY bug is worth fixing — multi-tile is where they CLUSTER, not a filter

The multi-tile emphasis above is about where bugs concentrate, NOT a
restriction on what to fix. When auditing or stumbling on a defect,
multi-tile or not, investigate and fix it (e.g. the shallow-copy undo
corruption and the resistance/stacking questions found in audit-pass-5 are
footprint-independent). Do not dismiss a bug because it isn't about
footprints. Two corollaries the user stated explicitly:
- **If a fix's intended behavior is ambiguous, ASK the user — don't guess.**
  A wrong "fix" that drifts from intent is worse than a question.
- **If CLAUDE.md's wording was ambiguous about the behavior in question, it
  MUST be amended** as part of the fix, so the ambiguity doesn't recur.

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

### Do audit/interaction passes YOURSELF — don't delegate to weak survey agents

**User directive (standing, as of the audit-pass era):** for correctness /
interaction audits on this system, do the work DIRECTLY — read the code, trace
the cross-cutting paths, and write numeric/behavioral assertion harnesses
yourself. Do NOT fan the audit out to a swarm of survey subagents running
weaker models (e.g. Haiku). They are no longer sufficient for this task: a
giant interconnected engine is the opposite of "obvious," and that's exactly
where they fall down. They're fine for catching shallow, LOCAL issues (a
missing import, a typo, "does X exist") — not for the interaction bugs that
are the entire point of these passes.

The evidence from the passes themselves: the swarm largely reported "clean,"
while the bugs that actually mattered came from doing it by hand —
`damage_spread`'s fragment-mode `NameError` (caught by a numeric harness, not
an agent), the turn-order skip-loop crash + round inflation, the mount
push/pull footprint bug, and the load-side deepcopy class. The agents'
serialization "findings" were false positives that cost verification time.

Yes, doing it yourself is more token-expensive. The user has explicitly said
that's the right trade: **more results per pass beats cheaper passes.** So:
- Read the relevant subsystems end-to-end and reason about how they compose.
- Write throwaway numeric/end-state assertion scripts for every gnarly
  primitive (damage_part, the modifier fold, damage_spread, clamps, geometry
  — see the prior passes for the pattern). The harness only catches `💥`;
  YOUR assertions catch wrong answers.
- Reserve subagents for genuinely parallel, mechanical, LOCAL lookups, not for
  holding the whole model in their head.

### Don't blindly trust agent recommendations

If you DO use an Explore or general-purpose agent for a narrow lookup, treat
its output as a lead, not a conclusion. The agents get things wrong — they'll
claim features don't exist when they do, miss key context, recommend features
that already shipped, or flag false-positive "bugs" (e.g. the pass-11
watchers/bound_channels shallow-copy claims, which were verified safe).
**Verify every agent claim against the code yourself before acting or
presenting it to the user.** Past survey agents have hallucinated ~30% of
their recommendations.

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
free-text questions; they're easier to answer. EXCEPTION (user
directive, restated more than once): for OPEN-ENDED "what feature
should I build / which idea next" questions, ask in PLAIN TEXT prose,
NOT the questionnaire — the menu of ideas needs room to explain each,
and the user wants to answer in their own words. Use the questionnaire
for bounded design choices (enum-shaped: which stat, which mode, which
enable-mechanism), not for picking a direction. Skip questions
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
  FUTURE slices the user may want: configurable side NAMES (gamerule).
  (entity-shape hitboxes + LOS-aware raycast SHIPPED — see the
  "Directional/vision geometry" entry below.)
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
- **Corpse STATUS introspection + corpse OCCUPANCY — SHIPPED.** Two corpse
  follow-ons (the deferrals tracked since the corpse-var / corpse arc).
  (1) **Status introspection:** `corpse_status_has(eid, name)` (bool, never
  raises on a missing corpse), `corpse_status_get(eid, name, path[, default])`
  (dotted field of a frozen status; raises on missing corpse/status/path
  UNLESS a default, mirroring corpse_var + status_get), and
  `corpse_status_names(eid)` (loopable, sorted, []=missing) read a DEAD
  entity's frozen statuses from the corpse snapshot's `status` dict (the
  "did it die cursed?" / "raise with the same affliction" patterns). The
  status analog of corpse_var; read-only. Core in formula.py
  (`_corpse_status_dict` + the three prims).
  (2) **Occupancy as a gamerule:** the `corpse_block_condition` rule (formula
  EXPRESSION, default "" = corpses passable, the old behavior). Plugged into
  `Match.cell_blocks` (so it flows through `_check_block` → every movement
  verb + the same block_walk/tp/push/swap toggles as tile/zone blocking).
  Bindings: `self`=mover, `tile_x`/`tile_y`=the corpse cell, `corpse_id` (NEW
  HOOK_CONTEXT name — read frozen vars via `corpse_var(corpse_id, ...)`),
  `corpse_team`. A cell blocks if ANY corpse covering it (large corpses block
  their whole footprint via `corpse_cells`) evaluates truthy. Fail-OPEN
  (malformed / missing-var → not blocking) like the rest of the block system,
  so gating vars need a real value on the MOVER (`!defvar` only defaults new
  spawns). Gated on the rule being set = zero cost when off. Scenarios
  447-448.
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
  `fire_status_tick` in logic.py. Scenarios 390-391. DEFERRED then SHIPPED in
  the status cluster below (cross-status interactions + resistance/immunity);
  damage-buff scaling of applied level/duration is still composable via
  `on_status_added`. This was framed as the modest precursor to the bigger
  combat layers (damage pipeline, action economy, reactions) surfaced by
  analyzing the three FCS combat-system docs.
- **Status cluster: tags + cross-status + resistance + counters — SHIPPED.**
  Four interlocking deepenings of the rich-status system (scenarios 449-452).
  - **Tags / categories (84):** a status DEFINITION carries a `tags` list
    (`!status tags <name> <csv|->`). The category other features key on. Prims:
    `status_tags(name)` (loopable, declared order), `status_has_tag(eid, name,
    tag)`, `statuses_with_tag(eid, tag)` (loopable, sorted — loop it to purge
    every 'debuff'). Tags are DEFINITION-level (a def-less status has none).
    `Match.status_def_tags(name)` is the accessor.
  - **TOKEN convention (shared):** a "token" is a bare status NAME or
    `tag:<x>` (matches any status whose def carries that tag). `Match.
    _status_token_matches(token, name)` is the single matcher behind removes /
    blocked_by / immune / resist.
  - **Cross-status interactions (17):** declarative def fields `removes` and
    `blocked_by` (CSV of tokens; `!status removes`/`!status blockedby`). On
    apply (in `apply_status`, BEFORE the stacking math): if the target has any
    `blocked_by` status → no-op; after an accepted application, statuses
    matching `removes` are cleared (fires even on a no-change refresh; never
    self-removes). "What freeze does is stored in freeze." on_status_added is
    still the escape hatch for richer logic.
  - **Resistance / immunity (18):** SOURCE-GATED like modifiers. Rule
    `status_resist_sources` (CSV roots, default `equipped`; per-entity
    `__status_resist_sources` replace / `__status_resist_sources_add` extend)
    + the direct innate `status_immune`/`status_resist` entity vars. A nested
    `status_immune` (list/CSV of tokens) or `status_resist` (map token->int
    level reduction) found under a scanned root contributes — so an EQUIPPED
    ring resists, an inventoried one does NOT. Immunity (any matching immune
    token) blocks outright; resistance reduces the applied LEVEL (duration
    untouched for now), and if it drops to <=0 the application is fully
    resisted (no-op). Multiple reductions combine per the `status_resist_stack`
    rule (sum default / max / first). Core: `Match.status_resistance(eid,
    name)` -> (immune, reduction); `_gather_resist_records` +
    `_effective_status_resist_sources`. Prims `status_resist_of` /
    `is_status_immune`; read-only `!status resist <eid> <name>`. The existing
    PARTS immune/redirect (`part_status_immune`) is a SEPARATE part-only
    mechanism that still runs first in `apply_status`.
  - **Universal counters (87):** `status_counter_add(eid, name, delta[,
    field="duration"])` and `status_counter_set(eid, name, value[, field])`
    adjust ANY numeric field on a live status instance and auto-remove the
    status at <=0 — the same tool for time-based DURATIONS and per-trigger
    CHARGES (charges differ only in that nothing auto-decrements them from the
    turn clock; the GM calls status_counter_add(-1) from whatever the trigger
    is). Also `!status counter <eid> <name> <add|set> <value> [field]`. Auto-
    removal goes through the status diff chokepoint (`_status_remove`).
  - apply_status now also surfaces a block reason to the command layer via
    `Match.status_apply_block_reason(eid, name, level)` (immune / blocked by X
    / fully resisted). All def fields serialize (deepcopy); resistances are
    plain entity vars; counters are instance data.
- **Builder/objectives/transfer/layers bundle — SHIPPED (scenarios 453-457).**
  Four small composable features.
  - **Line/fill tile builder (49):** `!tile line <x1> <y1> <x2> <y2> <path>
    <value>` and `!tile fill ...` stamp ONE path=value across many cells in a
    single command (`line` = the engine's `_line_cells` segment geometry,
    `fill` = the bounding rectangle). Pure sugar over `!tile set` (same
    path=value shape — run twice for glyph+block on a wall). Off-grid cells
    skipped + reported.
  - **Toggleable map layers (114):** `Match.hidden_layers` (serialized set;
    layers `zones`/`tiles`/`entities`/`fog`). `!map layer <name> on|off` (host-
    gated via ELEVATED_ARGS) persists; `!map layer list` shows state; a one-off
    `!map hide=zones,fog` arg (player-available) suppresses layers for a SINGLE
    render without mutating state. Threaded as `render_ascii(..., hidden_layers=)`
    → `_render_ascii_impl(hidden=)` (each layer loop gated; fog overlay too). NOT
    a Discord-only feature — works in the CLI/harness. `full` stays honored only
    as args[0] so `hide=` can't sneak a player past the fog gate. (No coords/axis
    layer — infeasible with 1-char cells.)
  - **Cross-match entity transfer (107):** `MatchManager.copy_entity(src_mid,
    dest_mid, eid, x, y, move=)`. `!ent copy <id> <dest_match> [x y]` duplicates
    into another LIVE match (keeps source); `!ent transfer ...` MOVES it (note:
    `!ent move` is the movement verb, so transfer is the rename). Full fidelity:
    vars/statuses/passives/clamps/facing + the whole body-part SUBTREE (BFS,
    parents before children; `part_of` remapped; glued/region parts re-stamped,
    located parts keep their offset). Colliding ids auto-suffixed (`goblin` →
    `goblin_2`). Routes through `Entity.spawn` (fires on_entity_spawned in the
    dest, validates placement). A part can't be transferred alone (move its
    parent). Template-save-for-later is a SEPARATE future PR (cross-match
    permanent storage); this is the direct match→match move.
  - **Match outcome / victory (100):** NO built-in objective evaluator (the user
    chose primitives over a `Match.objectives` table). `Match.outcome` (None =
    ongoing, else `{winner, reason, round}`; serialized) + `Match.declare_winner
    (winner, reason)` / `clear_outcome`. Formula prims `declare_winner(winner[,
    reason])` / `match_winner()` / `match_over()` are MATCH funcs, so they fire
    from ANY formula context — watcher effects, actions (Exodia auto-win),
    on_death passives (boss slain), tile `on_enter` hooks (goal tile), zones,
    status ticks, etc. Commands `!match win <winner> [reason]` (manual) / `!match
    win clear` (resume) / `!match outcome` (read-only, player-available). Winner
    shown in the `!state` header. "Victory is declared manually" is the default;
    win conditions are COMPOSED, not configured.
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
    part of:"; `!part list` shows hp + knobs. Scenarios 396-398; further coverage 414 (composite 2×2 with region head + torso), 415 (detach → free entity), 416 (parent death snapshots parts + revive restores), 417 (killing a part = limb destruction, never a corpse — `_process_death` routes a part to `_process_part_death`; only non-parts corpse). Multi-level part SUBTREES (a part of a part, e.g. dragon→wing→feather — the shape `copy_entity`/transfer already walks via BFS) are now handled on death/revive too: `entity_part_subtree(root)` (BFS, parents before children) drives both the death cascade (the WHOLE subtree is removed, no deeper-limb zombie) and the corpse snapshot (whole subtree stored in parent-before-child order, so revive re-spawns each after its parent and re-attaches the tree). Scenario 467.
  DEFERRED TODOs (the user explicitly wants these tracked):
  - **AoE damage SPREAD between main and limbs — SHIPPED (scenario 410).**
    `damage_spread(target, total[, mode, fragments])` → to-main; splits total
    across the target's parts (DIVIDED, never full-to-each — no free AoE
    headshots), each routed via damage_part. Modes (rule `aoe_default_mode`):
    `weighted` (per-part `aoe_weight` var, defaulting to summed `hit_weights`,
    else `part_aoe_weight_default`), `uniform`, `fragment` (N=`aoe_fragment_count`
    discrete weighted-random hits, match-RNG), `main_only` (hit main hp
    directly). No parts / zero weights → full total to main. Largest-remainder
    apportionment so shares sum to total. GM loops `entities_in_area` + calls
    it per entity (falloff stays GM-side). SPATIAL origin/radius filtering is
    the NEXT PR (with footprint-region part positioning).
  - **Status effects on body parts — SHIPPED (scenario 411).** Two list rules,
    each overridable per part (the `__status_immune` / `__status_redirect`
    vars replace the rule when set): `part_status_immune` (apply_status no-ops
    these on a part) and `part_status_redirect` (applied to a part → applied to
    the PARENT instead, for per-entity DoT). Hooked into `apply_status` only
    (raw `!ent status` editing force-writes). Parts are real entities, so
    statuses otherwise tick on them normally — a part's tick can `damage_part(
    self, n)` to route to main.
  - **Independently-LOCATED parts — SHIPPED (scenario 409).** A part with the
    `__part_located` var keeps its OWN cell: `Entity.is_located_part` /
    `is_glued_part` (the new skip-surface predicate — glued parts only).
    A located part is NOT re-stamped to the parent and NOT hidden — it
    renders, occupies (occupancy enforced on placement), is targetable, and
    sees/is-seen; it still routes damage / resolves `parent` / dies + revives
    with the parent (revive restores it at its own stored cell). `!part
    locate <part> <x> <y>` / `!part glue <part>` (+ Match.locate_part /
    glue_part). Parent move does NOT drag it. Per-cell independent TARGETING
    (selecting the part by clicking its cell) still TBD per game system.
  - **Multi-tile AoE + footprint-region part positioning — SHIPPED (scenarios
    412-413).** (1) Spatial AoE: `damage_spread(target, total, mode, fragments,
    origin_x, origin_y, radius)` filters to parts with a cell within `radius`
    (Chebyshev) of the origin — a blast that doesn't reach the whole body
    (no eligible parts → full total to main). (2) `part_region` (the
    `__part_region` var, set by `!part region <part> <region>`): a part's cells
    become a facing-aware REGION of the parent's footprint —
    `front`/`back`/`left`/`right`/`center`/`all` + corners. Derivation
    (`Match.part_region_cells` + `_region_match`) PROJECTS each footprint cell
    onto the parent's forward/right axes (`FACING_VECTORS`) and selects by
    SIGN, so it's full 8-way (a diagonal facing → a non-rectangular set);
    `center` falls back to ALL on an even (no-true-center) footprint. The cells
    are an explicit set returned via `entity_cells` (the single chokepoint, so
    render/occupancy/AoE/vision all pick it up); the anchor follows the parent
    (`_restamp_parts_for` restamps glued + region parts), cells re-derive each
    call from the parent's live facing. `Entity.is_region_part`; `is_glued_part`
    (the skip-surface predicate) now excludes BOTH located and region parts.
    region and `!part locate` are mutually exclusive (each clears the other;
    `!part glue` clears both). (3) Render priority: the `part_custom_glyph_priority`
    rule (default True) — a region part draws over the parent only if it has a
    CUSTOM glyph (a default-glyph region part yields, so it doesn't clobber the
    parent's customization); done in a second render pass. Located parts (own
    cell, no overlap) are unaffected.
  - Per-damage-TYPE `to_main_percent`; the **armor layer** (coverage % +
    directional, damage-type AR-vs-ARP mitigation); the **to-hit roll**
    (accuracy/evasion/suppression/spread, SEPARATE from hit-location); **AP/FP/
    ARC action economy + reactionary actions** (block/dodge → the reaction
    framework the choice-replay system seeded); fancier revive (regrow from
    template).
  - **Snake / segmented bodies — SHIPPED (scenarios 418-422).** A SEGMENT is a
    LOCATED part (own cell — renders/occupies/targetable) that also FOLLOWS the
    head along a chain. Linkage: segments are parts of the head, each carrying
    `__segment` + `__follows` (= the segment/head directly ahead);
    `Match.snake_segments(head)` walks the chain head→tail, `is_snake_head`
    detects a head. **Follow** (rule `segment_follow_mode`, head-var override
    `__segment_follow`): `trail` (default) — each segment moves into the cell
    the one ahead just vacated (always adjacent), driven per-cell from
    `fire_entity_step`; `path` — the head's cell path is recorded
    (`__seg_path`) and segments sit `segment_spacing` cells back (gaps). A
    discontinuous head move (tp/swap/push — no per-cell steps) is detected in
    `fire_entity_moved` via a stale `__seg_last` and re-lays the body straight
    behind the head (`_resettle_snake`). **Self-collision** (rule
    `segment_self_collision`, default False = pass-through, the Destroyer): the
    head ignores its OWN segments for occupancy via `Match._occupancy_ignore`
    (threaded through move_dirs / tp `_validate_placement` / push / pull /
    swap); True = blocked by its body (classic Snake). Other movers are always
    blocked by segments. **Death/sever** (rule `segment_death_mode`, segment-
    or head-var override `__segment_death_mode`), applied in
    `_process_part_death` → `_sever_segment` when an own-hp segment is
    destroyed: `solid` (Destroyer — segments are 0/0 indestructible routing to
    main, never individually die, whole snake dies with the head); `cascade`
    (destroying a segment removes it + every segment BEHIND it, no corpses);
    `split` (Eater of Worlds — the segment behind the cut is PROMOTED to a new
    independent head via `_promote_segment_to_head`: clears the part/segment
    linkage, stamps `segment_split_head_template` [head-var override
    `__segment_split_head_template`, dotted-fill of MISSING vars via
    `_fill_missing_vars`], inherits the old head's initiative; trailing
    segments re-parent to it; the cut segment is removed → one worm becomes
    two). Built on the part-corpse invariant (killing a segment is limb
    destruction, never a corpse). Authoring: `!part segment <head> <id> <name>
    <hp> <maxhp> [k=v ...]` appends to the tail; the summon-template `segments`
    list/dict chains a body at spawn. Serializes free (linkage is vars +
    `part_of`). FUTURE (all low-priority / deferred): spacing>1 in `trail` mode
    (currently always adjacent; `segment_spacing` only applies in `path` mode)
    — the user is fine with the current spacing. Branching (non-linear, TREE)
    bodies — a hydra / multi-tail where several segments share one `__follows`
    predecessor; structurally invasive (chain walker + sever become subtree
    ops), the user is NOT interested yet. NOTE explicitly OFF the table:
    autonomous AI for split-off heads (or any entity) — see "NO automated
    entity behavior" in §1; a promoted head is a complete independent UNIT
    (own initiative + the stamped template's actions/passives), but it never
    acts by itself.
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
    records (each carries a `source` label). Read-only `!mod show <eid> <stat>
    [base] [tag ...]` renders the active modifiers (with [source]) + folded
    result (context-dependent ones show only when their condition resolves
    context-free). Scenarios 399-400; further coverage 407 (set/min/max ops + priority tiers), 408 (defender context role + scaling value formula).
  - **C1 follow-ups SHIPPED (scenario 406):** (a) `source` tracking —
    `_raw_modifier_records` returns (record, source) labels like
    `status:burning.fireboost` / `equipped.sword.0`; (b) per-stat caps — the
    `modifier_stat_caps` rule (CSV `stat:lo:hi`, lo/hi optional) clamps the
    FINAL value even with no modifiers (per-entity caps stay the min/max ops);
    (c) tag-granting — a record's `grants_tags` expands the query tag set in a
    single pre-pass (no chain-granting). FUTURE: more context roles. This is
    groundwork the combat refactor (damage types, armor AR-vs-ARP, to-hit)
    will lean on.
- **Text-renderer customization (glyphs + color) — SHIPPED (entities, v1).**
  Two ways to tell units apart in the ASCII renderer, both config-as-vars.
  - **Custom glyphs** (universal, no ANSI): `Match.entity_glyph(e)` resolves a
    per-facing `glyphs.<facing>` var > a direction-agnostic single `glyph` var
    > the default DIRECTION_ARROWS arrow (`@` fallback). Exactly ONE character
    (else ignored → falls through), same rule as tile/zone glyphs, so columns
    stay aligned. Works on every surface incl. the harness.
  - **Color** (surface-gated, ANSI): `TEXT_COLORS` maps names (red/green/.../
    bright_*) → ANSI SGR fg codes. Codes work on BOTH a terminal and Discord
    `ansi` blocks: Discord only supports 30-37 + style 1 (bold) and NOT the
    90-97 bright range, so the bright_* variants are `1;3X` (bold+base) and
    `gray` is `1;30`. `Match.entity_color(e)` = the entity's
    `color` var > its team's color (the per-match `team_colors` map, DEFAULTING
    to the team's own name when that name is itself a palette color — a team
    named "red" auto-renders red) > None. `render_ascii(pov, colorize=)` wraps
    each entity glyph in `\x1b[<code>m…\x1b[0m` when colorize. FG only for now.
  - **Surface plumbing:** the command layer colorizes only when
    `ctx.supports_color` AND the match's `color_enabled` (default True); it
    fences the map ` ```ansi ` so Discord renders the codes. Discord sets
    `supports_color=True`; the CLI sets it per-run via `_enable_terminal_color()`
    (turns on Windows VT processing via SetConsoleMode; respects NO_COLOR /
    non-tty; on failure → plain + a one-time "use Discord / glyphs" warning,
    so a legacy console never spews raw escapes); the scenario harness does
    NOT set it → its `!map` is always plain (scenarios stay clean). Per-match
    toggle `!map color on|off`; team
    map via `!map teamcolor <team> <color>|clear|list` (host-gated via
    ELEVATED_ARGS["map"]). Both fields serialized on Match.
  - **Guide + invalid-color warnings:** `!map colors` (read-only, player-
    available) lists the palette via `_color_guide()`; `!map teamcolor` with a
    bad color and `!ent set_var <id> color <bad>` both cite it — the latter is
    a non-blocking ⚠ advisory (still writes the var, since it may feed the
    GM's own formulas). The whole palette is Discord-safe, so there's no
    terminal-only color to warn about separately.
  - Scenarios 401 (glyphs) / 402 (color settings) / 403 (guide + warning).
  - **Colored TILES + ZONES — SHIPPED (follow-up slice).** render_ascii now
    carries a parallel `colors` grid (palette NAMES) painted alongside the
    glyph grid, layer by layer (zone < tile < entity < fog); each layer that
    owns a cell sets BOTH its glyph and its color (color None = uncolored),
    so the topmost feature owns the tint (keeps the positional layering), and
    a zone/tile with a color but NO glyph still tints its `.` ("fill empty
    cells"). `Match.tile_color(x,y)` (instance `color` data > template) and
    `Match.zone_color(name)` (zone `color` field) via the shared
    `_resolve_color_value`: a bare palette name is literal, anything else is a
    FORMULA EXPRESSION (bindings tile_x/tile_y or zone_name) whose string
    result must be a palette name — fail/non-palette → None (same fail-safe
    as visibility/block conditions). Surface: tiles use the existing `!tile
    set <x> <y> color <name|formula>`; zones get `!zone color <name>
    <name|formula|->` (a dedicated field like the zone glyph, serialized in
    _zone_to_dict/_from_dict). Both `!tile set color` and `!zone color` give
    the same ⚠ advisory as entity color on a non-palette, non-formula-shaped
    value (still stored — could be a formula). Entity color stays literal-var
    only (not formula) — unchanged. Scenario 404. Long-term someday: an
    actual image-rendered map.
- **Range-band primitive — SHIPPED (scenarios 423-424).** `band(value, spec,
  default=None)` (a pure `_ALLOWED_FUNCS` func) looks `value` up in a banded
  table `spec` ('1-2:120,3-5:100,6-9:80,10+:0'), FIRST match wins. Ranges:
  `lo-hi` (inclusive), `n` (exact), `lo+`/`lo-` (lo and up), `-hi` (up to hi).
  Result coerced to a number when numeric. No match → `default`, else raises.
  Models the doc's munition falloff without a conditional chain.
- **Reusable named macros — SHIPPED (scenarios 425-426).** `Match.macros` (name ->
  newline-separated command body). `!macro set/run/list/show/remove`. `run`
  substitutes $1/$2/.../$@ (positional; missing → ""; leaves $(...) formula
  tokens alone, via `_macro_subst`) then dispatches each line via
  `dispatch_no_snapshot` — so the whole macro is ONE undo entry (the !macro
  command itself is snapshotted). Per-match, serialized.
  - **Macro CONTROL FLOW — SHIPPED (scenarios 544-546).** A macro body is parsed
    (`_parse_macro` → a node tree, validated at `set` time) and executed by a
    small interpreter (`_exec_macro`), so a macro can branch and loop; a macro
    with no directives stays a flat command list (unchanged). Directives (own
    lines): `if <formula>` / `elif <formula>` / `else` / `end` (truthy formula
    runs the block, first match wins) and `repeat <count>` / `end` (loop the
    block `count` times). Blocks NEST. `$#` substitutes the current 1-based
    `repeat` iteration (empty outside a loop; added to `_macro_subst` alongside
    $N/$@). Conditions + repeat counts are evaluated with the SAME strict
    read-only gate as inline $() args (`formula.validate_arg_safe` + a read-only
    `eval_expression`) — a state-changing function in control flow is rejected.
    Safety caps: `macro_repeat_limit` rule (default 1000, clamps each repeat) +
    `macro_step_limit` rule (default 10000, hard total-dispatch backstop that
    aborts a runaway). The active match is re-fetched per condition/repeat eval
    (no stale reference if a macro line switches/restores a match). Still ONE
    undo entry.
- **Condition-watchers — SHIPPED (scenarios 427-428).** `Match.watchers` (name ->
  {condition, effect, once, last}). EDGE-triggered: `Match.fire_watchers`
  evaluates each condition (a formula expr), records all edges, then runs the
  effect (a formula program) for any that went false→true; `once` removes
  after firing; malformed condition reads as not-met (fail-safe). Polled by
  `CommandRegistry.run` AFTER each top-level command settles (single pass —
  an effect's change is caught next poll, no loops; turn/round are covered
  since they're commands). `!watch add <name> "<cond>" "<effect>" [once]` /
  remove / list / show / check. `last` serialized so a reload doesn't
  re-fire. Distinct from event passives: fires on the condition's transition
  regardless of what changed it.
- **Team-level state (resources + modifiers + passives) — SHIPPED (scenarios
  429-431).** `Match.team_data` (team -> free-form dict) + `team_passives` (team
  -> {pid: Passive}). (1) Resources: `team_get/team_has/team_set/team_add`
  (Match methods + formula prims; dotted paths) and `!team set/get/add/list/
  clear`. (2) Team-scoped MODIFIERS: a `modifiers` bundle in a team's data
  (e.g. `!team set red modifiers.rally.op add`) is aggregated by
  `_raw_modifier_records` for every member (source `team:<team>`), so it
  flows through apply_mods. (3) Team-scoped PASSIVES: `!team passive add
  <team> <pid> <when> <formula>` — fire for any member (self = the member) via
  the new `Match._firing_passives(target)` helper, which yields global + the
  target's team passives and replaced the raw `global_passives` iteration at
  every fire site. All serialized.
- **Aliveness as a rule + indestructible-0/0 render fix — SHIPPED (scenarios
  418, 432).** `Entity.is_alive` was a hardcoded `hp > 0`, so an
  INDESTRUCTIBLE 0/0 entity (a passthrough body part / zone — e.g. a Destroyer
  segment routing all damage to main) read as DEAD and was silently dropped
  from render AND occupancy (you could walk through the worm's body). Fixed +
  generalized into the `alive_condition` rule (formula expr, `self`=the
  entity, distinct from `death_condition` which drives the death PIPELINE):
  EMPTY (default) = the built-in `hp > 0 OR is_indestructible` (the fix); set a
  formula to REPLACE it (include the carve-out yourself via the new
  `is_indestructible(eid)` formula primitive). Evaluated only when set (default
  stays on the fast path), recursion-guarded via `Match._alive_eval_depth` (a
  condition that calls an is_alive-using enumerator falls back to built-in),
  malformed → built-in (never blanks the board).
- **`body_part_entity_line_suffix` rule — SHIPPED (scenario 433).** A
  SUB-ENTITY's `!list`/`!state` row now appends a suffix naming its parent;
  default `" [part of {parent}]"`. Rendered in `_entity_line` only when
  `e.is_part` (parent alive); placeholders `{parent}` / `{parent_name}` plus
  every entity_line_format key (resolved against the part). Empty = off. Only
  parts on the roster (located / segment / region) show it — glued parts are
  hidden anyway.
- **Directional/vision geometry — SHIPPED (scenarios 434-436).** Three
  primitives extending the directional + LOS + footprint layers.
  (1) **Box-face (footprint-aware) side_hit** — `side_hit` / `directional_get`
  / `hit_location` now judge a MULTI-TILE target's struck side against its
  real rectangle, not a center POINT. The `side_hit_hitbox_mode` rule
  (`box` default | `center` legacy; per-call `hitbox=` override) selects it:
  `box` aspect-corrects the center→attacker vector by the footprint
  half-extents (`dx/hx, dy/hy`) and takes the bearing of that NORMALIZED
  vector, then runs it through the existing `_relative_angle` +
  `_relative_side_name` pipeline — so a hit along a long flank reads `side`
  (not `front`) even near a corner, and 4/8-way + corner_arc + any facing all
  keep working with no special diagonal case. 1×1 is byte-identical to
  `center` (uniform correction when w==h), so the default-on change only
  affects multi-tile bodies. (2) **`raycast(x1,y1,x2,y2[,viewer])`** —
  `Match.raycast`, the IMPACT point of a beam: the farthest clear cell before
  terrain stops it (or the target if clear), as an (x,y) read via
  coord_x/coord_y. The companion to `first_opaque` (which returns the
  BLOCKER); raycast returns where the beam LANDS. Walks the shared
  `_line_cells`; the cell adjacent to an opaque origin-neighbour yields the
  origin. (3) **Render vision memo (perf)** — `_fog_team_sees` is memoized via
  a transient `Match._vision_memo` dict, activated only around `render_ascii`
  (now a thin wrapper over `_render_ascii_impl`) and torn down in `finally`,
  so the per-cell-per-layer fog scan (which loops every team member with a
  per-member LOS walk) isn't recomputed. NEVER held across a mutation — lives
  only for one synchronous read pass — so it can't go stale; renders are
  byte-identical with it on/off. Cross-command caching intentionally out of
  scope (avoids invalidation hazards).
- **Custom event bus — SHIPPED (scenarios 437-439).** A GM-extensible hook
  surface on top of the fixed HOOK_NAMES, decoupling cause from effect.
  Handlers are ordinary PASSIVES whose `when` is `event:<name>` (`is_event_hook`
  relaxes the when-validation in Passive.__post_init__ + the !passive /
  !gpassive / !team passive command checks). Emission: the `emit(name,
  payload=None, target=None)` formula prim and the `!emit <name> [to=<eid>]
  [k=v ...]` command. `Match.emit_event` fires GLOBAL handlers ONCE (self =
  target if given, else the current-turn entity, else None); when a `target`
  is given it ALSO fires that target's TEAM + OWN handlers (self = target) — a
  DIRECTED event. Broadcast-to-many is the GM looping emit per target (cause
  stays explicit; no implicit fan-out). The `payload` dict is read inside a
  handler via `event_get(key[,default])` / `event_has(key)` (from a transient
  `Match._event_stack`, NOT a binding — sidesteps the sandbox's no-dynamic-
  attribute rule); `event_name` is a HOOK_CONTEXT binding. Re-entrancy (a
  handler that emits) is capped by the `event_recursion_limit` rule (default
  64) with a var-hook-style warning latch (`_event_warnings`) drained by the
  outermost emit. emit works from any formula context (action body, watcher
  effect, another handler). All transient (`_event_depth`/`_event_stack`/
  warnings not serialized); the subscribing passives serialize as normal
  passives. The foundational primitive several other approved features
  compose on.
- **Combat helpers: shields + chain/bounce — SHIPPED (scenarios 440-443).**
  Two new primitives where assembling them from existing pieces was painful
  (the sandbox has no damage-interception or loop+visited-set); channeled /
  charge-up were judged COMPOSABLE and shipped as demonstration scenarios
  only (no new engine code), per "do we really need a whole feature for X?".
  - **Shields / temp-HP** (the `temp_hp_sources` rule, CSV of vars roots,
    default `shields`; per-entity `__temp_hp_sources`): named absorb POOLS
    (`shields.plate`, `shields.ward`, ...) so multiple independent layers
    coexist. A pool is `{amount, priority?, tags?, not_tags?}` or a bare
    number. `absorb_damage(eid, amount, tags=None)` drains matching pools
    HIGHEST priority first (ties by name), removes any emptied to 0, mutates
    the pool vars (firing their var hooks), and RETURNS the leftover that
    PENETRATES to HP — the GM applies it (`entity[t].hp = entity[t].hp -
    absorb_damage(t, dmg)`). Tag match mirrors modifiers: an untagged pool
    absorbs anything, a `tags` pool only hits carrying those tags (a typed
    ward), `not_tags` excludes. `shield_total(eid, tags=None)` sums available
    absorb (no mutation). Pools are plain vars — set/refresh via `!ent
    set_var`; DECAY is GM-composed (a status tick / round hook), not an engine
    feature. Core in formula.py (`_gather_pools` / `_absorb_damage` /
    `_shield_total`).
  - **Chain / bounce** — `chain_targets(from_eid, count, max_jump=0,
    relation="")` (loopable): up to `count` entity ids, each the nearest alive
    eligible entity to the PREVIOUS link, never revisiting, within max_jump
    Chebyshev cells (0/None = unlimited); `relation` reuses the
    any/hostile/ally/same_team/attackable filter (`_candidates`/`_relation_ok`).
    The GM loops it and owns the per-hop falloff (engine never auto-applies
    damage). Near→far.
  - **Channeled (#60) / charge-up (#61) — DEMONSTRATED, not built.** Scenario
    442: a `channeling` flag + an `on_entity_moved` passive (break on move) +
    an `on_var_changed target=hp` passive (break on damage). Scenario 443: a
    `charging` flag + `charge` counter advanced by an `on_turn_start` passive
    that releases the wound-up attack at a threshold and resets. Both prove
    the engine already supports the pattern via status/var + hooks + use_action
    + the event bus — no new primitive warranted.
- **Dice DSL extensions + weighted tables — SHIPPED (scenarios 444-446).** The
  pre-existing `roll("NdM±k")` primitive (a shared `_roll_impl` used by both the
  unseeded and the random_seed-bound bindings) gained per-die-group suffixes:
  `!` (EXPLODE — a die on its max face rolls again and adds, capped at
  `_ROLL_EXPLODE_CAP`, skipped for sides==1), and `kh<n>`/`kl<n>` (KEEP highest
  / lowest n of the rolled dice — `2d20kh1` = advantage, `2d20kl1` =
  disadvantage). Grammar via `_ROLL_DIE_RE` / `_ROLL_FLAT_RE`; dice are rolled
  into a list (post-explosion per die) then keep-filtered. New
  `roll_table(spec)` (match-bound, replay-safe via `_active_rng`): a weighted
  random PICK returning a key — input is a `"key:weight,..."` CSV (weight
  optional, default 1) or a `{key: weight}` dict; 0-weight entries never chosen;
  the discrete-choice companion to `band` (which buckets a NUMBER into a range).
  Both honor `random_seed` for reproducible sessions.
- **Mounts / vehicles (slots + riders) — SHIPPED (scenarios 458-461).** A
  VEHICLE is just an entity carrying a `slots` var (no hardcoded type); a SLOT
  def lives at `vehicle.vars.slots.<name>` (author with `!ent set_var`). Slot
  fields: `capacity` (numeric budget, default 1), `cost` (per-rider formula
  consuming it, default "1"; `self`=rider + `vehicle` binding), `condition`
  (valid-rider formula gate; fail-OPEN), `region` (a facing-relative footprint
  region — reuses the part-region machinery via the factored
  `Match.region_cells_of` — at which the rider RENDERS and is targeted; absent =
  hidden inside), `controls_movement` (any number of slots may set it — an
  occupant drives), and `actions` (a slot-scoped action bundle). The rider
  back-link is the protected Entity fields `mounted_on` / `mount_slot` (serialized,
  like `part_of`); occupancy is DERIVED by scanning (`vehicle_riders` /
  `slot_occupants`), no second structure. Core on Match: `vehicle_slots` /
  `slot_def` / `is_vehicle` / `slot_capacity` / `slot_cost_of` /
  `slot_used_capacity` / `slot_condition_ok` / `can_mount`→(ok,reason) /
  `mount_entity` / `dismount_entity` / `switch_slot` / `rider_cell` /
  `_restamp_riders_for` / `_release_riders`.
  - **Movement.** NO hardcoded "drive" action (the GM writes movement actions).
    A rider's own move (`Entity.move_dirs` / `tp`, via `_mount_move_redirect`)
    is REDIRECTED to the vehicle when its slot has `controls_movement` (the
    whole rig moves; riders are carried), or REFUSED with a clear message when
    it's a plain passenger ("dismount first"). Moving the vehicle directly
    carries everyone: `_restamp_riders_for` is hooked into `fire_entity_moved`
    alongside the part/anchor restamps (raw move_to — no rider hooks fire, like
    parts). push/pull/swap operate on the vehicle (riders are excluded from
    occupancy so they're never the push target).
  - **Skip surfaces.** A HIDDEN rider (no region) is excluded from ground
    occupancy (`cell_occupant`), render, POV visibility (`entity_visible_to`),
    and the spatial enumerators (`_candidates`/`all_entities`/`entities_in_area`)
    — the part-glued skip surface. A VISIBLE (region-slot) rider draws OVER the
    vehicle at its region cell (a dedicated render priority pass) and stays
    targetable; ALL mounted riders are off the ground (they share the vehicle's
    cells). Riders keep their own turn/initiative + hp + actions; they show in
    `!list` with a `mount_entity_line_suffix` suffix. Multiple visible occupants
    spread across the region's cells by index.
  - **Slot/vehicle actions.** A rider's available actions are augmented by
    `discover_mount_actions`: (1) actions defined in its slot
    (`slots.<slot>.actions.*`, container_path == `slots.<slot>`), and (2)
    vehicle-wide actions carrying an `allowed_slots` field (list/CSV; `*`/`all`
    = any) whose list includes the rider's slot. Other slots' actions and
    vehicle-private (no allowed_slots) actions are NOT offered. Merged in
    `_run_action_dispatch` + shown in `!action list`. Run-as is configurable:
    the `mount_action_actor` rule (`rider` default | `vehicle`) with a
    per-vehicle `mount_action_actor` var override decides which is the action's
    `source`/actor; BOTH `vehicle` and `rider` ids are ALWAYS bound in the body
    (new HOOK_CONTEXT names `vehicle`/`rider`/`slot`). In rider mode the action's
    container is dropped (source = the rider's plain vars; read vehicle/slot
    config via `entity[vehicle]`). Threaded via a new `extra_ctx` param on
    `run_action`.
  - **Lifecycle.** New hooks `on_mounted` / `on_dismounted` (fire on the rider,
    bind `vehicle`+`slot`; switch fires dismount-then-mount). Host death/despawn
    runs `_release_riders` from `Entity.remove` per the `mount_on_host_death`
    rule (`eject` default — dismount to nearby free cells via
    `_find_dismount_cell` | `kill` | `keep`). Formula prims: `mount` /
    `dismount` / `switch_slot` (mutating), `is_mounted` / `mount_of` / `slot_of`
    / `is_vehicle` / `riders` (loopable) / `slot_riders` (loopable) /
    `slot_capacity` / `slot_free` / `can_mount`. Command `!mount <rider>
    <vehicle> <slot>` / `dismount` / `switch` / `list` / `info` (list/info
    player-available via READ_ONLY_SUBCOMMANDS). All serialized.
    FUTURE the user may want: per-rider footprint inside a vehicle, edge-aware
    boarding range (mount only from an adjacent cell), nested vehicles' shared
    fuel/initiative, and an armor layer for riders-inside (positional cover).
  - **Mount bug fixes (scenarios 465-466).** (1) NESTED carry: a vehicle that
    is itself a rider now carries its OWN cargo when the rig moves —
    `_restamp_riders_for` replays fire_entity_moved's carry-restamp trio
    (anchors/parts/riders) for each moved rider, recursing down the stack
    (cycles are can_mount-guarded), so a rider-on-a-cart-in-a-gunship follows
    the gunship. (2) A rider that DIES revives UNMOUNTED: `_store_corpse` strips
    `mounted_on`/`mount_slot` from the snapshot, so revive_corpse no longer
    restores a phantom-mounted entity (an invisible "rider" on the ground / a
    free re-seat). It can mount again afterward.
- **Map viewport (panning) + auto-legend + auto-update boards — SHIPPED
  (scenarios 463-464; #110 + #111 + #24).** The Discord-surface map block.
  - **Viewport / panning (#110, headless-testable core).** Caps how much grid
    renders at once: engages when EITHER dimension exceeds its cap (a 70×5 grid
    still windows horizontally), window size = min(cap, grid) per axis. Caps are
    the `viewport_width` / `viewport_height` rules (default 30). Per-CHANNEL
    offset in `Match.channel_views` (channel_key -> [x,y], serialized), the
    panning analog of per-channel POV. Surface gating: the `viewport_mode` rule
    (`auto` default | `on` | `off`) — `auto` defers to the surface's
    `ctx.viewport_capable` flag (Discord True, CLI/harness False), so the CLI
    shows the whole map unless forced `on`. Core on Match: `viewport_engaged`,
    `_viewport_dims`, `resolve_viewport(channel_key, enabled=)`→(vx,vy,vw,vh)|
    None, `set_view` / `center_view` / `pan_view` / `clear_view` (all clamp the
    window to the grid). `render_ascii(..., viewport=(x,y,w,h))` clips the
    composition loop to the window. Commands: `!map pan <dir> [n]` (exact n
    tiles, default 1), `!map center <eid>` / `!map center <x> <y>` (camera to an
    entity/coord — clamped), `!map view <x> <y>` / `!map view reset`. pan/center/
    view are per-CHANNEL camera state so they stay player-available; the bare
    `!map` shows a "viewport (...)" header + pan hint when windowed.
  - **Auto-legend (#111, headless-testable).** A glyph→meaning key appended
    under the map, built from a parallel `meanings` grid populated only when
    `legend=True` (so it reflects the FINAL top-layer glyph at each cell and
    ONLY cells in the rendered window — POV/fog/viewport-correct). Entities →
    name, tiles → template name or "tile", zones → "zone: <name>", fog →
    "fog (unseen)"; grouped by glyph, row-major scan order. Per-match toggle
    `Match.map_legend_enabled` (seeded from `map_legend_by_default`, default
    off; serialized), command `!map legend on|off` (host-gated) + one-off
    `legend=on|off` arg.
  - **Auto-update boards (#24, Discord-ONLY — can't be harness-tested).** A
    self-refreshing map message per channel, edited in place after every
    command instead of re-posting. Lives in discord_commands.py (`_boards`
    registry, `_board_render`, `_refresh_boards_for_match`, `_PanView` arrow
    buttons, `DiscordCtxWrapper.set_autoupdate`); the post-dispatch refresh is
    hooked in `_dispatch` (single + batch) and the approval re-dispatch. A
    change in one channel refreshes every board on the same match. `!map
    autoupdate on|off` (host-gated) — surface-agnostic handler calls the
    optional `ctx.set_autoupdate` hook, so the CLI/harness report it as
    Discord-only rather than erroring. Arrow buttons pan by `viewport_button_step`
    tiles (0 = half-screen). NOTE: boards are runtime-only (Discord Message
    handles don't serialize) — re-issue after a restart. Minimap (#110's other
    half) was explicitly skipped.
- **Audit-pass-2 bug fixes (scenarios 468-473).** A second interaction-bug
  sweep (zones / fog-LOS / movement subsystems). Fixed:
  - **`move_group_dirs` now validates the whole footprint + blocking** (was
    anchor-only). Phase 1 checks every swept-footprint cell for bounds + the
    `block_walk` condition each step, and the FINAL footprint for occupancy
    (footprint-aware `cell_occupant`, fellow group members treated as
    transparent via `_occupancy_ignore(extra=...)`) — so a multi-tile member
    can't march off-grid / onto another body's non-anchor cells, and a group
    can't walk through an impassable tile/zone/corpse. Mirrors the
    single-entity `Entity.move_dirs` contract. Scenarios 468-469.
  - **LOS-only vision casts from the whole body** (`_entity_has_los` /
    `_team_has_los`). They used the anchor cell only, disagreeing with
    `_member_sees` (which checks every footprint cell) — so a large viewer's
    `can_see_losonly` / `team_sees_cell_losonly` was wrong. Now ANY footprint
    cell with a clear line counts. Scenario 470.
  - **`_restamp_parts_for` carries a part's auras + sub-parts.** It snapped a
    part's position but never re-stamped that part's anchored aura or its own
    sub-parts (a part-of-a-part) — same class as the nested-mount bug fixed in
    #80. Now walks the whole part subtree (BFS via `entity_part_subtree`,
    parents first) and re-stamps each moved part's auras. Scenario 471.
  - **`hidden_rider_grants_vision` rule (default False).** A hidden rider
    (passenger in a region-less slot) was excluded from being SEEN but still
    contributed to its team's vision/fog — an asymmetry. Now gated by the rule
    via the shared `_vision_member_ok` (used by `_team_sees` / `_team_has_los`
    / `_record_vision`); default off = symmetric (a passenger grants no
    sight). An explicit per-entity `can_see(<rider>,...)` is unaffected.
    Scenario 472.
  - **`resize_grid` shifts `channel_views`.** Resize repositions all
    coordinate-bearing content by the anchor offset but had missed the
    per-channel viewport CAMERA (added after resize was written), so a
    center/edge-anchored resize left the camera framing the wrong region. Now
    offset like everything else (resolve_viewport re-clamps on read).
    Scenario 473.
- **Audit-pass-3 fix: attached parts share the parent's TURN CLOCK (scenarios
  474-477).** Three per-unit "clocks" iterated only `turn_order` members (round)
  or the active entity (turn). Attached parts carry no initiative (excluded from
  turn_order), so a glued/region/located part's statuses, turn/round passives,
  and turn-scheduled effects NEVER fired — silently contradicting the doc
  ("parts tick normally; a part's tick can `damage_part(self,n)` to route to
  main"). Fix shape (user-approved): a part rides its parent's clock. The shared
  `Match._attached_tick_parts(base_targets)` BFS-walks each base target's part
  subtree and returns the parts that LACK independent initiative, STOPPING
  descent at an independent part (it's its own target and ticks on its own turn,
  carrying its own sub-parts) — deduped so a deep part isn't double-counted and
  an independent part reached via both its own turn-order slot and its parent
  isn't double-ticked. Wired into all three clocks:
  - **Statuses:** `fire_status_tick` appends the helper's parts to its targets.
    Each part's own definition `tick_when` still gates whether it fires.
  - **Turn/round passives:** `fire_hook` gained an `own_only_targets` param —
    those ids fire ONLY their entity-owned passives, NOT match-wide globals or
    team passives (which already fired once per acting unit, so they must not
    re-run per part). The six `on_turn_*`/`on_round_*` calls in `next_turn` /
    `_advance_index` pass `own_only_targets=self._attached_tick_parts(...)`.
  - **Turn-scheduled effects:** the two `fire_scheduled_turn(cur/new_cur)` sites
    also call it for each attached part.
  So a DoT/regen/bleed on a limb both lives on the limb and (via
  `damage_part(self,n)`) can bleed into the main body. (Also re-audited and
  found correct: `Match.to_dict`/`from_dict` round-trips every persistent field
  — `pending_requests` is intentionally runtime-only — and push/pull/swap are
  footprint-aware for multi-tile bodies.)
- **Audit-pass-3 fix: action rollback preserves ALL runtime-only state
  (scenario 478).** `action._rollback_match` restores a failed action's
  transaction by rebuilding the Match from the pre-state snapshot and copying
  its fields back, then re-applying a curated list of runtime-only (underscore)
  fields the snapshot doesn't carry. That list had gone STALE — it missed the
  event-bus fields (`_event_stack`/`_event_depth`/`_event_warned`/
  `_event_warnings`) and others (`_summon_count`, `_death_processing`,
  `_death_check_suppressed_ids`, `_alive_eval_depth`, `_vision_memo`,
  `_turn_order_dirty`, `_request_seq`, `pending_requests`). The headline crash:
  an action that `emit()`s an event whose handler runs a FAILING sub-action —
  the sub-action's rollback wiped the LIVE `_event_stack` (holding the outer
  emit's frame), so the handler's next `event_get` and the emit's own cleanup
  hit "pop from empty list", crashing the whole outer action. Fix: preserve the
  COMPLETE set of runtime fields (rollback only restores SERIALIZED state;
  transient in-flight state — the emit stack, summon budget, etc. — must
  survive). The list must stay in sync with Match's underscore fields; a
  `hasattr` guard makes a future-missing name a no-op rather than a crash.
  COMPANION fix in `run_action`: because `_summon_count` is now PRESERVED across
  a rollback, the choice-REPLAY loop (which rolls back + re-runs the body per
  interactive `choose`) resets it to the action-start value each attempt —
  otherwise a summon-before-`choose` would accumulate the per-command summon
  budget across replays and falsely hit `summon_event_limit`. Snapshotted
  alongside the existing per-attempt RNG/cursor/buffer resets.
- **Audit-pass-3 fix: a turn_end tick that empties the turn order no longer
  crashes next_turn (scenario 479).** PRE-EXISTING (parts-independent): if a
  `turn_end`/round hook or status tick removed the LAST entity in `turn_order`
  (e.g. a lethal DoT on the only combatant — now also reachable via a part tick
  routing `damage_part` to its vital parent), `next_turn` then computed
  `(active_index + 1) % len(turn_order)` against an empty order →
  ZeroDivisionError (surfacing as a 💥). Guarded every point a hook/tick can
  empty the order: `_advance_index` bails if `turn_order` is empty; `next_turn`
  returns `(None, log)` after the opening round_start, after `turn_end` hooks,
  and after `_advance_index`'s round-wrap ticks; `_skip_to_eligible` stops on an
  emptied order and clamps a stale `active_index`. A two-combatant table where
  one self-kills still advances cleanly to the survivor.
- **Audit-pass-3 enhancement: `!mod show` flags unrecognized modifier ops
  (scenario 480).** The fold (`_apply_modifier_op`) treats an op outside the
  recognized set (`add`/`inc%`/`more%`/`set`/`min`/`max`, now the module
  constant `MODIFIER_OPS`) as a lenient ADD — convenient, but it silently
  swallows a typo like `inc` for `inc%` (a flat +N instead of a %). Behavior is
  UNCHANGED (still lenient-add, so no existing match breaks); `!mod show` now
  marks any such line with ⚠️ and appends an advisory naming the bad op(s) +
  the valid set, via `Match.unknown_modifier_ops(mods)`. Read-only diagnostic
  surface only — the fold itself doesn't warn (no clean channel mid-formula).
- **Polymorph / transform (115) — SHIPPED (scenarios 481-483).** An
  identity-preserving statblock swap. `transform(eid, template, stash_path=None)`
  / `revert(eid, stash_path)` formula prims (match funcs) + `!ent transform <id>
  <template_ref> [stash_path]` / `!ent revert <id> <stash_path>`. REPLACES name,
  vars (incl. actions + footprint), passives, clamps, status, and the attached
  part subtree; PRESERVES identity — id, position, facing, team var, and
  turn-order slot (the turnorder_var value) all carry over, so references,
  initiative, and allegiance survive. Core: `Match.capture_statblock` /
  `apply_statblock` / `transform_entity` / `revert_entity`. apply_statblock
  despawns the old parts (despawn, NOT death — no corpse), swaps the fields in
  place, re-mints + re-links the new statblock's `parts`/`segments` (handles
  both the summon-style {role: template} dict AND a captured full-subtree list,
  remapping part_of for multi-level limbs via `_apply_statblock_parts`), rebuilds
  turn order, and suppresses death checks across the swap window. HP carries per
  the `transform_hp_mode` rule: `percent` (default — preserve the fraction of
  max_hp), `keep` (current hp clamped to new max), or `full` (the target's own
  hp). REVERT DESIGN (user's call): the pre-transform statblock is stashed to a
  CALLER-CHOSEN var path (not a protected var), and revert reads that path — so
  the stash is an ordinary inspectable/editable var, transforms STACK (stash
  each to a different path, revert in any order, even skipping levels), and
  there's no hidden state. `template_ref` for the command resolves as a dotted
  var path on the entity (the summon_from convention — store a template, then
  transform into it) OR a live entity id to snapshot. Multi-tile is first-class:
  swapping footprint_w/h vars swaps the footprint for free (482), and part
  templates spawn/despawn their limbs across transform/revert. Both prims/
  commands are mutating → host-gated.
- **Fake-statblock / disguise (116) — SHIPPED (scenarios 484-485).** A
  DISPLAY-ONLY, POV-gated presented statblock (the decoy/illusion primitive).
  The `disguise_var` rule (default `disguise`) names an entity var holding
  `{name?, glyph?, glyphs?, color?, vars?: {...}}`. A viewer NOT on the entity's
  own team (and not omniscient) sees the disguise's name/glyph/color and its
  `vars` overlaid on the roster; the entity's own team and the omniscient/GM
  view see the truth. Engine MECHANICS (targeting, formulas, damage, `var_get`)
  ALWAYS read the real statblock — a disguise only changes what's RENDERED.
  Core: `Match._effective_disguise(e, pov_team)` (None = show real: gated on
  pov_team being a non-None, non-own-team viewer + a disguise var present) +
  `entity_glyph`/`entity_color`/`entity_display_name` now take an optional
  `pov_team` and consult it; `render_ascii` threads pov_team through the three
  glyph/color paint passes + the legend meanings; `_entity_template_context` /
  `_entity_line` take pov_team and overlay the disguise name + vars (disguise
  vars win over the computed hp/max_hp/team for display). Surfaces: `!map` /
  `!list` / `!state` (the board); use `!as view <team>` to preview a POV in the
  CLI/harness. `!find` deliberately stays on REAL names — it already ignores
  visibility/fog entirely (a search/GM tool). A moving/animated decoy or an
  illusion that fools enemy TARGETING is a GM composition on top (mechanics use
  real, so a true targeting-fooling illusion would need the deep-illusion
  variant, deferred).
- **`!find` spatial predicates + `!foreach` bulk-apply — SHIPPED (scenarios
  486-490).** Two composing query/QoL features.
  - **Spatial `!find` predicates.** `!find` already had var comparisons
    (`hp<20`, `team=red`, `var!=v`, dotted paths), `status:`/`group:`/`action:`;
    the only gap was SPATIAL, now `near:<eid>:<radius>` (within radius of an
    entity — the reference itself matches at gap 0) and `within:<x>:<y>:<radius>`
    (within radius of a coordinate). Both use the FOOTPRINT-AWARE nearest-cell
    gap, Chebyshev (square_radius). The gap math is now the single
    `Match.entity_gap_distance(e_ref, e_other, mode)` + `cell_entity_distance(x,
    y, e, mode)` over a shared `_rect_gap` (rectangle nearest-cell distance);
    `formula.py`'s inline `_ent_dist` (behind `entities_within`/`nearest_entity`)
    was refactored to route through `entity_gap_distance` so the enumerators and
    the `near:` predicate agree exactly. A malformed radius / missing reference
    RAISES `VTTError` from `_find_match_entity` — so `find_cmd` (and `foreach`)
    now run the match loop INSIDE the predicate-parse try.
  - **`!foreach <predicates> ; <command>`.** Runs ONE command per entity matching
    a `!find` selector (the selector reuses the exact find grammar). The bare `;`
    token splits selector from command (first `;` only; like `!batch`); the
    command runs once per match with `$id`/`$name`/`$x`/`$y` substituted
    per-token (`$name` LAST so an injected name isn't re-substituted). Matches
    are resolved to a fixed (id, name, x, y) snapshot BEFORE any command runs, so
    mutating the board mid-loop (move/kill/spawn) can't change the target set.
    ONE undo entry (foreach is snapshotted; inner commands go through
    `dispatch_no_snapshot`); a per-entity `❌` is reported and the loop continues
    (batch semantics). Host-gated by default (mutating) — a player can't wrap a
    mutating command to bypass the gate, since the inner ungated
    `dispatch_no_snapshot` is only reached after foreach passes the top-level
    gate. Helpers `_foreach_subst` + the `foreach_cmd` handler in vtt_commands.py.
    FUTURE the user might want: multiple commands per entity (extra `;`), a
    read-only `!foreach` variant, more substitution tokens.
  - **`!foreach` upgrades — SHIPPED (scenarios 557-558).** Two of the three
    flagged follow-ups above. (1) **Multiple commands per entity:** after the
    first bare `;` (selector separator), further bare `;` tokens split the tail
    into MULTIPLE commands; all commands for one entity run before the next
    (PER-ENTITY grouping, so a multi-step recipe reads top-to-bottom). Still ONE
    undo entry (the whole sweep is snapshotted; inner commands via
    `dispatch_no_snapshot`). Empty groups from a doubled/leading/trailing `;`
    are dropped (like `!batch`). New helper `_split_foreach_commands`. (2) **More
    substitution tokens:** `$team` (the entity's team var, "" if none), `$i`
    (1-based index in the matched set, TURN-ORDER order), `$n` (total match
    count), alongside the existing `$id`/`$name`/`$x`/`$y`. `_foreach_subst`
    stays a SINGLE-pass `re.sub` with the alternation ordered longest-first
    (`$name`/`$team`/`$id` before the `$i`/`$n` prefixes) so a substituted value
    containing a token isn't re-expanded and `$id` isn't eaten by `$i`. `$x`/`$y`
    remain the ANCHOR cell for a multi-tile entity (the addressing convention);
    the near:/within: selector stays footprint-aware. Host-gated as before (the
    inner ungated dispatch is only reached after foreach passes the top gate —
    no player bypass). The DEFERRED third piece — a player-usable READ-ONLY
    `!foreach` — was intentionally left out (it overlaps `!find show:/sort:`,
    which already gives players per-entity readouts, and it adds an
    access-gating surface worth a design decision first).
- **Audit-pass-4 fixes: multi-tile interaction sweep (scenarios 491-492).** A
  fourth interaction-bug sweep, this time hunting anchor-only assumptions in
  OLDER features against multi-tile entities (three read-only survey agents
  across zones/auras/tiles, vision/LOS/targeting, and AoE/spawn/corpse/mount;
  every flagged candidate verified in code before fixing). Two real bugs found
  + fixed; the rest of the surface re-confirmed footprint-correct.
  - **`move_group_dirs` fired tile/zone movement hooks at the ANCHOR cell
    only.** Group movement (`!ent move group:<name> ...`) validated the whole
    swept footprint (audit-pass-2) but then fired `on_enter`/`on_exit`/`on_stop`
    via the anchor-only `fire_tile_hook`/`fire_zone_*_hooks` instead of the
    footprint-aware `fire_footprint_tile_*`/`fire_footprint_zone_*` that
    `Entity.move_dirs` uses — contradicting its own docstring ("per intermediate
    tile … same as single-entity move_dirs"). So a multi-tile group member
    crossing a hazard band / zone edge under-fired hooks (a 2×2 walking over a
    damage strip burned once, not per covered cell). Now mirrors `move_dirs`
    exactly (per-step `old_cells`/`new_cells` via `entity_cells`); byte-identical
    for a 1×1 member, correct for a footprint. Group move ALSO now fires the
    per-step `on_entity_step` hook (after each cell's `on_enter`) that
    single-entity `move_dirs` fires — a pre-existing, footprint-independent
    parity gap (per-cell reactions + snake-trail follow now work under group
    move). Scenario 493.
  - **`entities_in_area(x, y, n)` measured distance to the ANCHOR cell.** The
    coord-rooted twin of `entities_within` used `_distance(x, y, e.x, e.y, mode)`
    while `entities_within` had been refactored to the footprint-aware
    nearest-cell gap — so a large body partly inside an AoE radius was wrongly
    excluded (a 4×4 at (10,10) missed a blast at (14,14) r2 because the anchor
    was 4 away though a corner cell was 1 away). Now routes through
    `Match.cell_entity_distance(x, y, e, mode)` (the point-vs-footprint gap added
    with the `within:` find predicate), so the entity- and coord-rooted area
    queries agree.
  - **Re-verified footprint-correct (no change needed), so future sweeps can
    skip them:** all vision/fog/LOS casts (`_member_sees`, `_entity_has_los`,
    `_team_sees_entity`, `_record_vision`, `entity_visible_to` — union/any-cell),
    targeting geometry (`side_hit`/`hit_location`/`directional_get` box-face
    hitbox, `entity_center`/`aoe_origin`), the spatial/LOS enumerators
    (`entities_within`/`nearest_entity` via `entity_gap_distance`,
    `entities_in_cone`/`_rect`/`_line_ignorelos`/`_on_los`/`_line_until` via
    `_alive_at`/`_occupants` any-cell), `damage_spread` spatial filtering,
    `chain_targets`, `summon_near`/`_find_free_cell_near` (whole footprint
    validated, defaults applied first), corpses (`corpse_cells`/`revive`),
    mounts (`_find_dismount_cell`/`_restamp_riders_for`), `resize_grid` (cut if
    ANY cell off-grid), and single-entity `tp`/`move_dirs`/push/pull/swap.

- **Audit-pass-5 fixes: general correctness sweep (scenarios 494-495).** A
  broader bug hunt (NOT multi-tile-scoped — four read-only survey agents across
  serialization/undo, status/modifier/event, death/corpse/transform/mount, and
  action/formula/dispatch; every candidate verified in code before fixing). Two
  real bugs fixed:
  - **`Entity.to_dict` shallow-copied `vars` → corrupted undo + action
    rollback for nested vars.** `to_dict` did `"vars": dict(self.vars)` (shallow)
    while the sibling `status` was `deepcopy`'d. Entity vars hold nested dicts
    (`inventory`, `modifiers`, …) that `_set_path` mutates IN PLACE, so a command
    snapshot SHARED the live nested objects; a later dotted-path write then
    corrupted the snapshot, and `!history undo` / transactional action rollback
    restored the wrong (mutated) value (a nested var could even vanish entirely).
    Fixed to `copy.deepcopy(self.vars)`. This is the snapshot path behind BOTH
    undo and `action._rollback_match` (both go through `Match.to_dict` →
    `Entity.to_dict`), so it fixes both at once. Footprint-INDEPENDENT — a
    long-standing latent bug any nested-var undo would hit.
  - **`_restamp_parts_for` didn't carry a moved part's RIDERS.** When a parent
    moved, the part-restamp synced each glued part's anchor + its anchored auras
    (`_restamp_anchors_for`) but never `_restamp_riders_for(part)`, so a body
    part that is ALSO a vehicle left its riders behind (same class as the
    nested-mount bug #80, but for a part-vehicle). Added the symmetric
    `_restamp_riders_for(e.id)` call (recursion stays bounded — part subtree is
    acyclic, mount cycles are can_mount-guarded).
  - **Re-verified correct (no change):** Match/zone serialization round-trips all
    persistent fields; `action._rollback_match`'s runtime-field list is complete
    (audit-pass-3); modifier fold min/max ops; event-stack preservation across
    nested-action rollback; status counter auto-removal; formula sandbox
    `_who_arg` HOOK_CONTEXT handling + `normalize_body_source` at every body
    boundary; dispatch gate (no batch/foreach/macro/action bypass).
  - **OPEN QUESTION raised with the user → RESOLVED (status resistance +
    `add_level`).** The old gate `(name not in e.status or new_level is not
    None)` let an implicit +1 (`!status apply x poison` with no level) on an
    already-present `add_level` status BYPASS resistance, while an explicit
    level was resisted — an asymmetry. The user's call: keep `apply_status`
    consistently resistance-aware AND add a SEPARATE force primitive that
    ignores resistance (resistance stays LEVEL-only — no duration channel).
    Shipped (scenarios 496-497):
    - **Resistance is now mode-aware** via `Match._resistance_applies(e, name,
      sdef, level_given)`: a flat level-reduction resistance applies only when
      a level is actually added/set — a FIRST application, an `add_level`
      increment (implicit +1 OR explicit), or a `replace` with an explicit
      level. `refresh`/`extend`/`none` set no level, so resistance no longer
      touches them (fixes BOTH the implicit-+1 bypass AND a previously-possible
      bug where an explicit level on a `refresh` ran the resistance gate and
      could no-op the duration refresh). So an implicit `add_level` +1 with
      resist≥1 is now fully resisted (consistent); `status_apply_block_reason`
      shares the same helper so command feedback matches.
    - **`force` path** — `apply_status(..., force=True)` skips the immunity +
      resistance gating entirely (the level/increment lands regardless);
      cross-status `blocked_by` and the part immune/redirect rules are STILL
      honored (force is specifically the "ignore resistance" axis, not a
      bypass-everything hammer). Surfaced as the `status_force(eid, name[,
      level, duration])` formula primitive (twin of `status_apply`) and the
      `!status force <eid> <name> [level] [duration]` command (host-gated like
      apply; reply reads "Force-applied").

- **Audit-pass-6 fixes: cross-subsystem correctness sweep (scenarios 498-506).**
  A sixth bug hunt (four read-only survey agents across transform/disguise,
  dispatch/foreach/macro/watcher/undo, dice/modifier/shield numerics, and
  mounts/vehicles; every candidate verified in code before fixing). Nine real
  bugs fixed, three of which needed a user design call:
  - **Snapshot shallow-copy, tiles + zones (HIGH).** `Match.to_dict` stored each
    tile's data dict BY REFERENCE and `_zone_to_dict` stored a zone's
    `data`/`hooks` by reference (only `cells` was rebuilt). Since `tile_set_path`/
    `zone_set_path` (and the `tile_set`/`zone_set` primitives) mutate IN PLACE,
    an in-place `!tile set`/`!zone set` corrupted the prior command snapshot —
    defeating undo change-detection (pre==post → no snapshot) AND action
    rollback. Same class as the audit-pass-5 `Entity.to_dict` vars fix, missed
    for tiles/zones. Fixed with `copy.deepcopy` in both serializers (498-500).
  - **Segment `__follows` not remapped on id re-mint (MED-HIGH).** Both
    `_apply_statblock_parts` (transform/revert of a captured subtree) and
    `copy_entity` (cross-match copy/transfer) remapped `part_of` but NOT the
    snake-segment back-pointer `__follows`, so a 2+ segment snake lost its chain
    past the first link when re-minted under an id collision. Fixed by remapping
    `__follows` via the same idmap in both paths (505; verified under a forced
    collision — worm→worm_2, s1's `__follows` s0→s0_2).
  - **Dice `kh0` negative-zero slice (HIGH).** `dice[-0:]` is the WHOLE list in
    Python, so `roll("NdMkh0")` returned the full sum instead of 0 (the `kl`
    branch `dice[:0]` was fine). Guarded `k==0` explicitly (498).
  - **Macro / foreach substitution (MED/LOW).** `_macro_subst` re-expanded a
    token appearing INSIDE an arg value ($@ pass then positional pass) and
    mis-parsed `$10`+ (the `$1` prefix). `_foreach_subst` only guarded
    `$name`-contains-token, not an id/x/y value containing a later token. Both
    rewritten as a SINGLE-pass `re.sub` (501; macro now also supports $10+).
  - **Mount `slot` binding (MED, two sites).** The mount-action dispatch
    `extra_ctx` never set the documented `slot` binding (read as None), and
    `_eval_slot_expr` hard-wired `slot` to the rider's CURRENT mount_slot (None
    on a fresh mount, stale on a switch) instead of the slot being EVALUATED.
    Fixed: bind `slot` in the action ctx; thread the evaluated slot param through
    `_eval_slot_expr` from `slot_cost_of`/`slot_condition_ok` (506).
  - **chain_targets relation anchor (user call → ORIGIN).** `relation`
    (hostile/ally/…) was judged vs the PREVIOUS link each hop, so a `hostile`
    chain flipped allegiance (enemy→ally→enemy). Now judged vs the ORIGIN
    `from_eid` (distance still measured from the previous link), so chain
    lightning bounces among the caster's enemies (502).
  - **swap + mounts (user call → REDIRECT).** `swap_entities` had no mount guard.
    Now applies `_mount_move_redirect` to both participants: a driver (a
    controls_movement slot) redirects the swap to its VEHICLE (riders carried), a
    passenger raises "dismount first" — mirroring tp/move_dirs (503).
  - **transform of a vehicle with riders (user call → gamerule, default block).**
    Replacing a vehicle's `slots` var wholesale orphaned its riders. New rule
    `transform_rider_mismatch_mode` (enum block|eject, default `block`): if EVERY
    rider's slot still exists in the new form they stay mounted; otherwise block
    (refuse, raise before any change) or eject (dismount all, then transform).
    `_release_riders` gained an explicit `mode` override for the eject path (504).
  - Re-verified CORRECT (no change): disguise POV gating + fog/footprint
    interaction, HP carry modes, modifier fold + caps + tag/grants, shields/
    absorb priority + drain, band()/roll_table boundary + weight handling,
    damage_spread apportionment, watcher edge-trigger + serialization, the access
    gate (no batch/foreach/macro bypass), nested-mount carry + cycle guards,
    rider-death corpse strip, `_find_dismount_cell`.

- **Active Time Battle (ATB) turn model — SHIPPED (scenarios 524-526).** An
  optional, SYSTEM-WIDE alternative turn engine (a plain rule, not a per-match
  toggle — a system is designed around it from the start). When `atb_enabled`
  is on, ROUNDS ARE DISABLED and `next_turn` no longer cycles `turn_order`:
  instead each alive turn-order member accrues CHARGE into a bar var
  (`atb_charge_var`, default `atb_charge`) at a per-entity RATE
  (`atb_charge_formula`, an EXPRESSION with `self`=the entity, default
  `entity[self].initiative` so initiative doubles as speed), and the next turn
  goes to whoever's bar fills soonest. Core: `next_turn` branches to
  `_atb_next_turn` → `_atb_select` (compute each rate, advance ALL bars by the
  minimum time-to-fill `(atb_threshold - bar)/rate`, the soonest entity is the
  actor, reset its bar) → `_atb_turn_phase` fires the actor's turn surface.
  DESIGN POINTS:
  - **Reset** via `atb_reset_formula` (program, `self`=actor; EMPTY default =
    built-in subtract `atb_threshold` keeping overflow). Read the target in a
    custom reset via the `atb_threshold()` prim.
  - **Each next_turn is ONE entity's turn.** A skippable (`skips_turn`) actor's
    turn ELAPSES — bar resets, status ticks STILL fire (so DoTs/stuns decay,
    since there are no round ticks under ATB) — but its action surface
    (tile/zone time-hooks, on_turn_* passives, schedule_on) does NOT fire. NO
    skip-loop: a fast-but-stunned unit burns frequent wasted turns while a slow
    unit charges (correct ATB), instead of starving the selection. `act` =
    not-skipped gates the action surface in `_atb_turn_phase`; status ticks are
    unconditional.
  - **Round-coupled formulas RAISE a visible FormulaError** under ATB:
    `round_number()`, `turn_index()` (it's a position-within-round), and the
    round-based `schedule(delay, ...)`. Turn-based `schedule_on(eid, ...)`,
    `turn_index`-free cadence, and all turn hooks/ticks keep working. A
    ONE-TIME ⚠ warning fires from `_atb_next_turn` (latched `_atb_round_warned`,
    reset when ATB is found off) if dormant round logic exists
    (`_has_round_logic`: on_round_* passives global/team/entity, round status
    ticks, round schedules, tile/zone on_round_* hooks).
  - The charge bar is an ordinary var (read via `var_get`, nudge via a haste
    formula / pre-fill for an ambush). New read prims `atb_threshold()` /
    `atb_rate(eid)`. No new serialized Match field (bar = a var, enable = a
    rule); `_atb_round_warned`/`_atb_last_skipped` are runtime-only (preserved
    across action rollback). Tiebreak on simultaneous fills: higher rate, then
    id. Rate <= 0 = the entity can't charge (excluded; all-zero → "no one can
    act"). FUTURE the user may want: per-entity threshold, a turn-elapsed
    counter to replace turn_index() under ATB, an ATB-aware `!state` readout.
    INTERACTION PASS (hands-on, all clean — scenario 527): verified ATB composes
    with death/self-kill mid-turn, mid-match summon (newcomer joins the charge
    race), attached-part status ticks on the parent's ATB turn, multi-tile
    actors, all-zero-rate (→ None + warning), mounts (rider + vehicle both
    rotate), undo (the `turn` flavor restores charge bars + the active actor),
    kill→corpse (round-internal `round_number` reads stay constant, no crash),
    random_stable tiebreak, and the ATB→round toggle (round play + round_number
    resume when atb_enabled is cleared). ONE v1 behavior to note:
    `transform`/`revert` REPLACES vars, so it resets the actor's `atb_charge`
    bar to the new form's value (absent → 0) — a fresh-form charge, NOT
    preserved like hp via transform_hp_mode. Defensible (new statblock) but
    flag if the user wants the bar to persist across a transform.

- **Status dispel + transfer — SHIPPED (scenarios 528-529).** Two status
  primitives built on the existing token machinery (`_status_token_matches` /
  `_token_list` / `_statuses_matching_tokens`) and the removal chokepoint
  (`_emit_status_diff(..., None)` fires on_status_removed).
  - **`status_dispel(eid, token, max=0)`** (Match.`dispel_statuses`) removes
    every status matching `token` — a name, a `tag:<x>` token, or a CSV of
    either — up to `max` (0 = all; capped removals go in sorted-name order).
    Returns the count removed; fires on_status_removed per removal. Design
    call (user): TOKEN-ONLY, NO 'undispellable' guard — keep un-strippable
    effects outside the token's range. Command `!status dispel <eid> <token>
    [max]`.
  - **`status_transfer(from, to, name)`** (Match.`transfer_status`) MOVES a
    status: it leaves the source unconditionally (on_status_removed) and
    RE-APPLIES on the destination via `apply_status` — so the dest's stacking
    mode + resistance/immunity/blocked_by all apply. Design call (user):
    RESISTIBLE move, consume-on-reject — if the dest resists/is immune the
    status is gone from the source AND doesn't stick (returns False). Carries
    level + duration; custom instance data RE-SEEDS from the definition (same
    behavior as the existing part_status_redirect, which also re-applies rather
    than byte-copying). Command `!status transfer <from> <to> <name>`. Both are
    mutating `!status` subcommands (host-gated); prims registered in
    `_MATCH_FUNC_NAMES`. (A future variant could preserve full instance data
    or be force/reflect-flavored.)

- **Graphics / sprite rendering — PHASE 1 SHIPPED (the engine render model;
  scenario 530).** The long-planned image-rendered map. CORE PRINCIPLE: the
  engine stays PIXEL-AGNOSTIC — a sprite is a KEY STRING stored in data
  (mirroring glyphs), and `render_scene()` emits a DECLARATIVE model that a
  graphics surface draws; the engine never loads an image. ASCII rendering is
  untouched (a parallel path), so all text surfaces/scenarios are unaffected.
  - **Sprite addressing** mirrors glyphs exactly: entity `sprite` /
    `sprites.<facing>` (resolved via `entity_sprite` → (key, flip_h, flip_v),
    disguise-aware like entity_glyph); tile `sprite` data > template (`tile_sprite`);
    zone `sprite` field (`zone_sprite`, set by `!zone sprite`); corpse sprite from
    the frozen snapshot (`corpse_sprite`); a per-match `background` ({sprite, mode},
    `!map background`, serialized) > the `background_sprite` rule (`background_layer`).
  - **Facing mirror**: a missing `sprites.<facing>` is filled by MIRRORING an
    existing facing (the `sprite_mirror` rule / per-entity var: none/horizontal
    [default = left↔right]/vertical/both); the model emits the chosen key + flip
    flags. Then the base `sprite`, then the `fallback_sprite` rule, then None
    (→ the surface renders the glyph as text).
  - **`render_scene(pov, hidden, viewport)`** → `{grid_w/h, viewport,
    background, placements[], fog[], borders}`. Each placement: `{kind
    (zone/tile/corpse/entity), ref, x, y, w, h, mode, sprite, glyph (text
    fallback), tint, opacity, flip_h, flip_v, layer}`. LAYERS: background(0) <
    zones(10) < tiles(20) < corpses(25) < entities(30) < riders/region-parts(40)
    < fog(50). Multi-tile entities carry w/h + `sprite_mode` (single/stretch/tile,
    rule + var). Corpses are greyed + semi-transparent (`corpse_sprite_tint` /
    `corpse_sprite_opacity`). Fog cells carry `fog_sprite` + `fog_opacity`;
    borders carry show/color/opacity + per-tile overrides (`border_color` /
    `border_opacity` data). Opacities are 0-100 (the schema has no float type).
    `render_scene` is PARALLEL to `_render_ascii_impl` (deliberately, to avoid
    risking the heavily-used ASCII path) but REUSES every predicate/resolver
    (visibility, POV, fog via `_fog_terrain_visible`, entity_cells, the
    glued/region/mounted/rider skip surface) — only the loop skeleton is
    duplicated; a keep-in-sync comment flags it. `!map scene` prints a textual
    summary of the model.
  - PHASE 2 SHIPPED: `gui.py` — a tkinter+Pillow desktop surface that runs the
    SAME commands as cli.py but DRAWS the `render_scene()` model. Split for
    headless-testability (tkinter is the ONLY non-testable part; it's thin glue,
    imported lazily): `SpriteLoader` (PNG-only, path-traversal-safe, cached —
    `sprites/` folder; extension + magic checked; `..`/absolute keys rejected;
    missing → None → glyph-as-text fallback), `SceneRenderer` (pure Pillow:
    background stretch/tile/center, placements in layer order, flip_h/flip_v,
    tint [`gray`=desaturate for corpses, a colour=multiply for teams], opacity
    [alpha-scale], multi-tile single/stretch/tile, glyph fallback, fog
    sprite/dark-overlay at fog_opacity, grid borders + per-tile overrides; cell
    size = the new `sprite_cell_size` rule, default 100), and `GuiCtx`/`GuiApp`
    (command Entry + log + Canvas; redraws after each command, reloading sprites
    so dropped-in art appears). **Pillow is an OPTIONAL dep, needed ONLY for
    gui.py** (engine/CLI/harness unaffected); there is no requirements.txt in
    this repo, so gui.py prints a `pip install Pillow` hint if it's missing.
    Run: `python gui.py [sprites_dir]` (needs tkinter + a display). The renderer
    is verified by PIXEL assertions in a headless test (loader security, sizing,
    sprite/tile/background pixels, stretch/tile, flip, tint, opacity, glyph
    fallback, borders+fog). NOTE one v1 behavior: `transform`/`revert` replaces
    vars, so a transformed entity's sprite naturally follows its new statblock.
  - PHASE 3 SHIPPED (the two parts the user wanted — #3 mouse select/drag and
    #4 animation stay deferred "for the foreseeable future"; scenario 531):
    - **(#1) Discord image attachments.** `sprite_render.py` was factored out of
      gui.py so the rendering (SpriteLoader + SceneRenderer, VERBATIM) lives in
      ONE surface-agnostic module with NO tkinter/discord dependency (loads +
      unit-tests headlessly). gui.py now IMPORTS `_PIL_OK`/`SpriteLoader`/
      `SceneRenderer`/`SPRITES_DIR_DEFAULT` from it (duplicate class defs
      removed — no drift). New `sprite_render.render_match_png(match, loader,
      pov_team=None, viewport=None, cell_size=None, max_dim=1600) -> bytes`:
      render_scene → SceneRenderer → downscale to `max_dim` longest side (0 =
      no cap) → PNG bytes; cell_size defaults to the `sprite_cell_size` rule.
      New command `!map image [full]` (vtt_commands.py) calls the OPTIONAL ctx
      hook `post_scene_image(m, pov)` via getattr — the set_autoupdate pattern:
      Discord implements it, the CLI/harness don't and report "graphics
      available on the Discord surface / gui.py" (so the harness exercises the
      gating path cleanly). `full` (omniscient) is host-gated in-handler
      (`m.is_host(ctx_user(ctx))`) since the dispatch gate only checks args[0]
      and here it's args[1]; otherwise channel POV. `post_scene_image`
      (discord_commands.py `DiscordCtxWrapper`) renders via a module-level
      cached `SpriteLoader` + `render_match_png` (in `asyncio.to_thread` — PIL
      is blocking), respects the channel's resolved viewport, posts a
      `discord.File(<id>.png)`, and returns "" so the image IS the reply (the
      handler suppresses an empty status). sprite_render imports are deferred so
      a Pillow-less host still loads the adapter (the hook reports graphics
      unavailable instead of crashing). Auto-update IMAGE boards (the graphics
      twin of the text `!map autoupdate`) deferred — `!map image` is one-shot.
    - **(#2) In-GUI pan/zoom.** gui.py's canvas got a toolbar (–/+/Reset zoom
      buttons + a % readout), scrollbars, and bindings: left-drag pans
      (scan_mark/scan_dragto), mouse wheel zooms (cross-platform: <MouseWheel>
      delta on Win/Mac, Button-4/5 on Linux), Ctrl +/- zoom, arrow keys scroll.
      Zoom is a GUI-LOCAL `_zoom` factor (0.25–4.0) that multiplies
      `sprite_cell_size` at render time (re-renders crisp, not a bitmap
      upscale); it does NOT touch the engine's per-channel viewport. tkinter
      glue stays untestable headlessly; the render path (render_match_png) is
      pixel-verified.
    - **GUI polish + default ground (follow-ups).** gui.py: the output log is
      READ-ONLY (state="disabled" except while inserting) so it can't be mistaken
      for the input; the command box is a MULTI-LINE Text (height 7) so a pasted
      block runs line-by-line (Enter runs all non-empty lines like a Discord
      !batch; Shift+Enter = literal newline; a Run button too). Default GROUND:
      the `background_sprite` rule now DEFAULTS to `ground_default` (mode `tile`
      via `background_mode`'s new default) — a tiled ground PNG from the sprites
      folder — so the map reads as terrain by default; if that PNG is missing,
      `SceneRenderer._draw_default_ground` paints a flat brown fill
      (`_GROUND_FALLBACK`) so cells never show as a black void. A per-match `!map
      background` or any loadable background sprite still wins (`_draw_background`
      returns whether it drew, so `render()` knows to fall back). gui.py layout:
      pack order = clipping priority, so input + log are packed at the bottom
      FIRST (reserved, never clipped — input first-class, log second) and the
      canvas frame packed LAST with expand, so on resize the MAP shrinks while
      the fields stay visible (default canvas 420px, `minsize(480,300)`).
    - **Default grid borders + per-match override (scenario 532).** The
      `show_borders` rule now DEFAULTS to True: white grid lines drawn ABOVE the
      ground/background but BELOW tiles/zones/entities (in `render()` the border
      pass moved to right after the ground draw, before placements — so a
      tile/entity sprite on a cell covers its border; the grid shows on open
      ground for alignment). Color/opacity from `border_color`/`border_opacity`
      rules, overridable PER-MATCH via new `Match.border_show`/`border_color`/
      `border_opacity` fields (each None = fall through to the rule; serialized;
      survive rule refresh) set by `!map border on|off | color <name> | opacity
      <0-100> | clear` (host-gated). `_scene_borders` resolves match field >
      rule, plus the existing per-TILE `border_color`/`border_opacity` data
      overrides (keyed 'x,y').
    - **Configurable sprite Z-layers (scenario 533).** The render-scene draw
      order per kind is now driven by the `sprite_layer_*` rules (defaults:
      background=floor < zone 25 < tile 50 < corpse 75 < entity 100 < rider 110;
      fog is always drawn last/on-top). Higher = on top; ties keep insertion
      order (stable sort), so default visuals are unchanged. Per-ITEM override:
      an entity's `sprite_layer` VAR or a tile's `sprite_layer` DATA field (a
      number) wins over the rule — `Match._coerce_layer` (junk → fall back to
      the kind default) + `_layer_rule`; applied in `_emit_entity_placement`
      (entity var) and the tile loop (tile data) in `_render_scene_impl`. The
      rider/region-part pass uses `sprite_layer_rider`. ASCII rendering is
      unaffected (z-layers are graphics-only). Default `border_opacity` was
      later lowered 100 → 50 (a subtle grid).
    - **Render-mode toggle + graphical auto-update board (scenario 534).**
      `Match.render_mode` (`text` default | `image`; serialized) set by `!map
      mode text|image` (host-gated). In `image` mode, a plain `!map` posts a
      rendered PNG instead of the ASCII block (via the same `post_scene_image`
      ctx hook as `!map image`), and `!map autoupdate` creates a self-refreshing
      IMAGE board; TEXT-only surfaces (CLI/harness, no hook) always fall back to
      ASCII. Discord board internals (discord_commands.py) are now mode-aware:
      `_board_image` (render_scene → PNG in `asyncio.to_thread`) + the unified
      `_apply_board` (edits content for text, or `attachments=[discord.File]`
      for image, falling back to text if Pillow is missing) drive
      `set_autoupdate`, `_refresh_boards_for_match`, and `_PanView._pan` (pan
      buttons now defer the interaction then re-render in place, so panning
      works for both modes). Boards follow the match's CURRENT render_mode on
      each refresh. All Discord-only (verified with stubbed discord objects, not
      the harness).
    - **Overlay sprites: status FX + entity overlay var (scenario 535).** A
      status (or a passive/action) can render a sprite OVERLAY drawn OVER an
      afflicted entity — the burning-overlay case. Two sources, both collected
      by `Match.entity_overlays(e, pov)` → `{sprite, opacity, tint, layer}`
      records, emitted as `kind:"overlay"` placements in `_emit_entity_placement`
      (so main entities, riders, and region parts all get them) over the
      entity's footprint + sprite mode: (1) the entity's active STATUSES — a
      status DEFINITION's `sprite`/`sprite_opacity`/`sprite_tint`/`sprite_layer`,
      OVERRIDDEN by the same field on the entity's status INSTANCE (set via
      `!status sprite <name> <key|clear> [opacity=] [tint=] [layer=]`); (2) the
      entity's `overlay_var` (rule, default `overlays`) — a dict of records
      (bare key string OR `{sprite, opacity, tint, layer}`), the path a PASSIVE
      composes by writing `entity[self].overlays.<name>.sprite = ...` (passives
      compose via vars, so no sprite field was bolted onto the Passive class).
      Default layer = `sprite_layer_overlay` rule (150, above entities at 100);
      per-overlay `layer`/`sprite_layer` overrides. DISGUISED entities (shown a
      decoy to the viewing pov) emit NO overlays so real status FX don't leak.
      Graphics-only — ASCII unaffected; no glyph fallback for overlays. Status
      def overlay fields serialize (deepcopy); the overlay var is a normal var.
    - **Sprites are SERVER-SIDE only — INTENDED (for now).** The bot does NOT
      accept sprite uploads over Discord (or any chat surface): there is NO
      inbound attachment handling anywhere (`bot.py` / `discord_commands.py`
      never read `message.attachments`, never download, never write user files
      to disk). Discord attachments are OUTBOUND only (the rendered map PNG from
      `!map image` / image auto-update boards). `SpriteLoader` is READ-ONLY
      (`Image.open` from the host's `sprites/` folder, path-traversal/PNG-only
      guarded); the only on-disk `.save()` in the codebase is `!save` (match
      JSON state), and render output goes to an in-memory `BytesIO`. This is a
      deliberate choice while the bot is self-hosted (e.g. on a laptop):
      validating the safety/legitimacy of arbitrary user-uploaded files on a
      publicly reachable bot is OUT OF SCOPE for now — each instance curates its
      own `sprites/` folder, so there is no untrusted-file ingestion surface. Do
      NOT add an in-chat sprite-upload flow without revisiting this decision
      (it would need validation/quotas/sandboxing). See `sprites/README.md`.

- **QoL commands: `!roll`, `!dist`, `!find` columns (scenarios 536-538).** Three
  small conveniences over existing primitives.
  - **`!roll <dice>`** — chat dice roller. `roll_detail(rng, spec)` was factored
    out of `formula._roll_impl` (which is now a thin wrapper) so it returns
    `(total, parts)` — `parts` being per-term breakdown strings — and the
    command shows the total PLUS the individual dice (`🎲 2d6+3 → 11 (2d6 [2,6],
    +3)`). Same grammar as the roll() primitive (NdM, +/-, explode `!`, kh/kl).
    Uses the match RNG (`m._rng or random`, replay-safe via random_seed) when a
    match is active, else global; works with no active match too. Read-only.
  - **`!dist <a> <b> [metric] [los]`** — distance: two entities, an entity + a
    cell (`!dist <eid> <x> <y>`), or two cells (`!dist <x1> <y1> <x2> <y2>`).
    Footprint-aware nearest-cell gap via `entity_gap_distance` /
    `cell_entity_distance` / `_rect_gap`; trailing `metric` (square_radius/
    chebyshev default, manhattan, euclidean) + `los` (anchor-to-anchor
    `has_los`). Read-only.
  - **`!find ... show:<csv> sort:<var>[:desc]`** — DISPLAY directives split off
    before predicate parsing: `show:hp,mp` appends chosen var values per row,
    `sort:hp` orders by a (dotted) var (`:desc`/`:asc`), missing var → `—` /
    sorts last. Filtering unchanged.
  - **Bonus bug fix:** `logic.py` never imported `math`, so the euclidean branch
    of `_rect_gap` (footprint nearest-cell distance, e.g. `entities_within` /
    `!dist ... euclidean` with the euclidean metric) raised `NameError` — a
    latent crash never exercised before. Added `import math`.

- **Inline `$()` formula args + `!reveal_fog` + named tables (scenarios
  539-543).** Three approved features bundled.
  - **Inline `$()` command args (the careful one).** A `$(...)` token in ANY
    command arg is evaluated as a READ-ONLY formula and substituted with the
    result (`!dist $(2+3) $(1+1) 10 10`, `$(entity[boss].hp/2)`).
    `resolve_arg_token` already did this for `!ent`; now the DISPATCHER
    (`CommandRegistry.run` + `dispatch_no_snapshot`) applies it to every
    command, gated by a `raw_args=True` registry flag that opts OUT the meta /
    self-handling commands (eval, batch, run, foreach, macro resolve `$()`
    per-line / in their own context; ent does its own self-aware pass).
    Substitution runs AFTER the access gate (a `$()` can't alter the gated
    subcommand) and only fires on `$(`-prefixed tokens (a stored formula body
    never starts with `$(`, so it's untouched). NOTE: shlex splits on spaces, so
    a `$()` containing inner quotes/spaces must be double-quoted; scenarios use
    quote-free forms (bare ids, `entity[id].path`).
    - **STRICT read-only safety** (the headline requirement): `$()` is parsed in
      EXPRESSION mode (assignments impossible → no entity writes) AND
      `formula.validate_arg_safe` rejects any call to a STATE-CHANGING function.
      The classification is EXPLICIT: `ARG_MUTATING_MATCH_FUNCS` (the banned set
      — kill/summon/var_set/status_apply/damage_part/emit/move_*/tile_set/
      zone_*/mount/team_set/log/…) vs `ARG_SAFE_MATCH_FUNCS` (an EXPLICIT
      read-only allowlist — NOT derived as all-minus-mutating, so default-DENY);
      `ARG_SAFE_FUNC_NAMES` = pure `_ALLOWED_FUNCS` + the safe match funcs.
      User-defined `!func`s are NOT allowed in args (unverifiable). A module-load
      **drift guard** asserts the two sets are DISJOINT and together cover EVERY
      `_MATCH_FUNC_NAMES` name, so adding a new match function without placing it
      in one set BREAKS THE BUILD — never a silent default-allow (when unsure,
      classify MUTATING). `FormulaError` (a `VTTError` subclass) surfaces as ❌,
      not 💥.
  - **`!reveal_fog <team> ...`** — reveal fogged cells to a team independent of
    unit vision (scout/clairvoyance/GM reveal); a revealed cell shows terrain
    AND live entities. Forms `all` / `at <x> <y> <r>` (Chebyshev disc) / `rect`
    / `around <eid> <r>` (footprint disc) / `clear`, optional `turns=N` for a
    TEMPORARY reveal (expires when `round_number` passes `until`; omit =
    permanent). `Match.fog_reveals` (team → list of `{cells, until}`; serialized,
    pruned lazily) ORs into `_fog_terrain_visible` + `_fog_entity_visible` via
    `_cell_revealed`. Host-gated; `list` player-available.
  - **Named random tables** — `Match.tables` (name → roll_table spec; serialized)
    via `!table def/roll/list/show/remove` + the `table_roll(name)` formula
    primitive. The picker (`formula.roll_table_pick`) was factored out of inline
    `roll_table` so both share it. `!table roll` is player-available (read-only
    RNG); def/remove host-gated. Replay-safe via the match RNG.

- **Container / inventory convenience primitives (scenarios 547-548).** The
  engine still has NO inventory concept; these are GENERIC var/container ops
  that make composing one (and counters, resources, loot) pleasant. Found by a
  hands-on "how hard is an inventory" pass — the friction was real (stacking-add
  needed a var_has conditional, transfer was read+write+del by hand, totalling a
  nested field needed a loop). New formula prims (formula.py namespace):
  - `var_add(eid, path, delta)` — create-or-increment a numeric var (the entity
    twin of team_add; the clean 'stack +N' / counter). MUTATING.
  - `var_move(src_eid, src_path, dest_eid, dest_path)` — MOVE a var/subtree
    between entities (deep-copied), returns True iff the source existed. The
    loot/give/drop primitive. MUTATING.
  - `var_sum_field(eid, path, field)` — sum a nested FIELD across a container's
    children (total weight/value), the companion to var_sum (direct children
    only). READ-ONLY (allowed in `$()` args).
  - `item_add(eid, path, amount=1, field=None)` / `item_consume(eid, path,
    amount=1, field=None)` — the AMOUNT-aware pair: add to / subtract from the
    container's amount FIELD (default the new `amount_field` rule = "amount",
    overridable per call). item_consume DELETES THE WHOLE CONTAINER at `path`
    when the amount hits <= 0 (the consumable 'use one, drop the empty stack'
    boilerplate). Both MUTATING.
  - New rule `amount_field` ("amount") — the default stack-amount key name.
  - Per the process rule below, all four mutating prims are in
    `ARG_MUTATING_MATCH_FUNCS` (banned in `$()` args) and `var_sum_field` is in
    `ARG_SAFE_MATCH_FUNCS`; the same pass converted `ARG_SAFE_MATCH_FUNCS` to an
    EXPLICIT allowlist so the drift guard truly forces classification.
  - Inventory authoring NOTES surfaced by the pass (worth remembering): an
    item's action has `source` rooted at the ITEM's container (`source.heal` =
    the item's heal), while `entity[self]` is the HOLDER — reach the holder via
    `entity[self].hp`, NOT `source.hp` / `entity[source]` / `entity[target]`.
    `var_get(source, ...)` fails (source is a proxy; use `self_id()`). DICT keys
    are unique, so two identical items can't share a top-level key — stack via
    an amount field, or use distinct keys (sword_1/sword_2); `!ent set_var hero
    inventory.potion 99` silently REPLACES the whole item dict (a footgun). A
    consumable's action vanishes with the item when item_consume removes it.

- **Audit-pass-16 (hands-on, recent-features sweep): two fixes (scenarios
  549-550).** A by-hand interaction pass over the recent work (containers,
  inline `$()`, reveal_fog, macros, overlays) with numeric/behavioral harnesses.
  Verified CORRECT (no change): item_consume transactional rollback on action
  fail(), var_move deepcopy isolation + dest-overwrite, reveal_fog memory/expiry/
  serialization, macro `if`/`repeat` read-only gate (enforced at RUN time —
  mutating conditions rejected, entity survives), container edge cases (clamp on
  var_add, non-numeric/missing paths, var_sum_field skipping non-dict children).
  Two issues:
  - **Vital-var deletion (HIGH, FIXED).** item_consume / var_del / var_clear
    could DELETE a vital var (hp/max_hp/initiative) → corruption (a missing hp
    reads as 0 → entity counts dead). The `!ent delete_var` command already
    blocked it and var_del/var_clear docstrings CLAIMED remove_var protected
    vitals — but it DIDN'T. Fixed at the chokepoint: `Entity.remove_var(path,
    allow_protected=False)` raises VTTError on a protected var, so every formula
    deletion path + the command are uniform; the two vital-var property setters
    (max_hp/initiative = None) pass `allow_protected=True`. var_clear's existing
    `except VTTError: continue` now actually skips vitals as intended.
  - **Inline `$()` POV info-leak → mitigated by a gamerule (user-approved
    default: a toggle).** Inline `$()` evaluates read-only formulas with NO
    POV/fog filtering, so a non-host PLAYER could read hidden entity vars via a
    player-available command (`!dist $(entity[boss].hp) 1 1 1` returned the
    fogged boss's exact hp). NOT a bug in the engine's formula model (formulas
    always saw all), but it undercuts fog-of-war info-hiding. New rule
    `inline_args_access` (enum all|host, default `all` = unchanged): set to
    `host` and a non-host's command containing a `$()` token is REFUSED
    (`CommandRegistry._inline_args_blocked` + `_has_inline_token`, checked in
    both `run` and `dispatch_no_snapshot` so the batch/macro inner-line vector is
    covered too). No-op on an open match (no owner), an auto-approve/identity-
    less surface, or for hosts. The same "tighten reads for a fog match" lever as
    `command_access`. Default stays permissive; a fog GM opts into the lockdown.

- **Audit-pass-23 (hands-on): CLEAN PASS — no bug found.** A by-hand sweep with
  exhaustive serialization checks, an event-bus review, broad input fuzzing, and
  numeric primitive re-verification; no code change. Verified combos (so future
  passes can skip re-grinding):
  1. **Serialization is airtight.** Built a match populating ~all 46 serialized
     fields (nested-var entities, multi-tile + parts + segments, status defs with
     overlay sprites + tags + modifiers, anchored zone, tiles, groups, aliases,
     macros, tables, watchers, team data + team passives, fog + memory + reveals,
     colors, layers, legend, border, render_mode, background, viewport). `to_dict
     == to_dict(from_dict(to_dict))` idempotent; and a LOAD-SIDE ISOLATION test
     (rebuild from a retained snapshot dict, mutate every nested structure on the
     loaded match IN PLACE, confirm the snapshot dict is untouched) passed — so
     `from_dict` deep-copies nested data, the passes-5/6/7/11 corruption class is
     fully closed.
  2. **Event bus (`emit_event`) is careful** — global handlers fire once; a
     directed event also fires the target's team + own handlers with existence
     re-checks (the passes-7/8 ghost-passive guards); `_event_stack` push/pop in
     try/finally; recursion capped by `event_recursion_limit` with a
     warning-latch drained at the outermost emit.
  3. **Input fuzzing (58 inputs) all handled cleanly (❌, never 💥).** 28
     numeric-arg commands fed non-numeric/huge/negative values (`!map resize/pan/
     center/view/border`, `!zone shift/fill`, `!tile line/fill`, `!part segment`,
     `!status apply/force/counter`, `!team add`, `!reveal_fog at/around`, `!ent
     hp/init/tp/add`, `!dist`, `!roll`, …) — the pass-18 int()-guarding holds
     broadly. 30 formula/structural edges (malformed passive/gpassive/func/clamp/
     status-tick/watch/action bodies, empty/vital-nesting var paths, vital
     deletion, ops on missing entities, self-referential alias/macro, a
     200-deep paren expr) — all clean.
  4. **Dice parser edge cases correct** — `1d1!`/`100d1!` don't infinite-loop
     (explosion skipped for sides==1), `0d6` is a clean FormulaError, `d6`
     implicit-1-die, combined suffixes (`2d6!kh1`, `3d6!kl1`), `kh` cap > dice
     count, negative groups — all exact.
  5. **Modifier fold is exact for the combat foundation** — status-instance +
     team + equipped sources aggregate together with correct `[source]` labels;
     `((10+5)+2)×(1+0.5)) = 25.5` to the decimal; a `modifier_stat_caps
     strength:0:20` rule (set before match creation so it's in the rules
     snapshot) clamps 110→20. NOTE: a status-instance modifier must be set via
     `!ent status <id> set <name> modifiers.<k>.<field> ...` (the status
     instance), NOT `!ent set_var <id> status.<name>...` (which writes a var
     named `status`, a different location — a false lead this pass).
  This is the third clean pass in the 20s (with 21); the harness-testable core +
  serialization + command-input robustness are solid. Likeliest remaining defect
  surface stays genuinely new code (as pass-22 showed — the bug was in the
  freshest change) and the Discord adapter.

- **Audit-pass-22 (hands-on): two "non-string → string-join" crash fixes
  (scenarios 558-559).** A by-hand sweep that (correctly) started with the
  freshest code — the just-added `!foreach` upgrades — and found a bug there,
  then a second of the SAME class elsewhere. Both are `💥`-level crashes where a
  non-string value reached a `", ".join(...)` / `re.sub` replacement that
  assumed strings:
  - **`_foreach_subst` `$team` (introduced by the foreach-upgrade change).** The
    `$team` token substituted the entity's team var without str-coercion (unlike
    `$i`/`$n`/`$x`/`$y`, which were `str()`d). A NUMERIC team var (`!ent set_var
    x team 5`) made the `re.sub` replacement lambda return an int →
    "sequence item 0: expected str instance, int found" (💥). Fixed by
    str()-coercing the replacement in the lambda (defensive for every token, not
    just `$team`). Regression added to scenario 558 (a numeric-team entity).
  - **`_tmpl_fmt_value` non-string DICT KEYS (pre-existing).** The `{placeholder}`
    template value formatter (used by `entity_line_format` / status-line / part-
    suffix templates) joined a dict var's keys with `", ".join(keys)` — but the
    sibling list branch already `str()`d its items, and the dict branch didn't.
    A formula can write a dict with non-string keys (`entity[a].loot = {1: 5}`);
    referencing that var in a template placeholder (e.g. `entity_line_format`
    `{loot}`) then crashed `!list`/`!state` with the same "expected str instance"
    💥. Fixed to `", ".join(str(k) for k in keys)` in both the truncated (>6) and
    full branches, mirroring the list branch. Scenario 559.
  Same-class sites deliberately LEFT (verified non-crashing or pathological-only):
  `!find sort:<var>` is type-safe by construction (a `(rank, number, string)`
  sort-key tuple, so mixed-type var values across entities never raise); the
  rule-name / slot-name joins (`logic.py` ~2545/6387) only take non-strings if a
  GM pathologically builds `slots = {1: ...}` with integer keys, which is far
  outside normal authoring. NOTE for future harness authors: the fastest bug this
  pass came from auditing the code I'd JUST written — fresh code is the highest-
  yield target, and a numeric team/var is a realistic GM input that scenarios
  rarely exercise.

- **Audit-pass-21 (hands-on): CLEAN PASS — no bug found.** A by-hand sweep
  targeting the recent-feature surface NOT already drilled in pass-20 (behavioral
  harnesses, no agent swarm); every check exact, no code change. Recording the
  verified combos so future passes can skip re-grinding them:
  1. **Container-prim vs clamp/vital interaction** — `var_add` on a clamped var
     RETURNS the pre-clamp computed value while STORING the clamped value; this
     is the SAME convention as `var_set` (both return the requested value, not
     the stored one) — NOT a bug (re-read via `var_get` for the stored value).
     `var_add`/`item_consume` route through `write_var`, so pass-18's vital-write
     coercion + the clamp system apply correctly (a write above max_hp clamps).
  2. **`ARG_SAFE_MATCH_FUNCS` classification is genuinely side-effect-free** —
     spot-audited the suspicious "safe" entries (`entity_snapshot` = pure
     template builder, `var_pick_random`/`roll_table`/`table_roll` = read-only
     except advancing the seeded RNG, which is intended/documented). No
     mis-classified mutator is reachable from inline `$()`.
  3. **`render_scene` vs `_render_ascii_impl` have NOT drifted** — both entity
     passes gate on `is_alive` + the SAME `entity_visible_to` / `tile_visible_to`
     / `zone_visible_to` / `corpse_visible_to` / `_fog_terrain_visible`
     predicates, skip the same glued/region/mounted parts, and are disguise-
     gated identically (the one deliberate difference: the scene region-part pass
     also honors a custom SPRITE, ASCII only a custom glyph). Overlay placements
     (`_emit_entity_placement`) are disguise-gated (no real-status-FX leak to an
     enemy POV) and follow the entity footprint/mode.
  4. **transform → save/load → revert round-trip** — a captured statblock stashed
     to a caller-chosen var survives `to_dict`/`from_dict` (form, part subtree,
     stash var all intact); revert on the RELOADED match restores the original
     name/hp/footprint and its inventory, and drops the transformed form's parts.
  5. **QoL commands** — `!find sort:<var>[:desc] show:<csv>` (missing var → `—`,
     sorts last; dotted paths), `!dist` (entity/cell/mixed, all metrics,
     footprint nearest-cell gap, `los`), `!roll` (NdM/kh/kl/explode, bad input →
     clean ❌). No crashes.
  6. **`roll_table_pick`** — weights validated (>=0, non-numeric → ❌), 0-weight
     filtered, cumulative selection sound (harmless dead `return total` after the
     final return; not worth a fix).
  7. **SpriteLoader path-traversal guard** — every `..`/absolute/mixed-separator
     key and non-PNG extension resolves to None (blocked); legit keys stay inside
     the sprites folder. (Defense-in-depth; sprites are server-side-only, no
     inbound upload path exists.)
  8. **reveal_fog** — a `turns=N` temporary reveal shows terrain AND live
     entities, then EXPIRES on the right round and is pruned from `fog_reveals`; a
     permanent reveal persists across rounds. Serializes + is footprint-aware
     (`around <eid>`).
  9. **Choice-replay through batch/foreach** — an action using `choose()`
     (transactional replay+rollback per choice) invoked inside `!batch` and
     `!foreach` with pre-supplied `answer=` tokens: a var write BEFORE the choice
     applies EXACTLY ONCE per invocation despite the replay, and the batch/foreach
     dispatch isn't corrupted by the rollback.
  Also re-confirmed clean by inspection: the Discord adapter board/approval/image
  logic (pass-18's per-match approval fix holds) and status dispel/transfer
  (tag dispel, max cap, consume-on-reject to an immune dest). NOTE for future
  harness authors: hp is clamped to `[0, max_hp]` and max_hp defaults to the
  spawn hp — so a bare `!ent add x X 30 ...` caps hp at 30; raise max_hp first or
  your "hp write is broken" repro is really the clamp working (this cost me a
  false lead this pass).

- **Audit-pass-20 (hands-on): macro runaway backstops — two DoS/crash fixes
  (scenarios 555-556).** A by-hand sweep of the least-audited RECENT features
  (container prims, inline `$()`, macro control flow, reveal_fog, named tables,
  overlays, the Discord adapter) with behavioral harnesses. Verified CORRECT (no
  change): the container/inventory prims (item_consume drop-at-0 / over-consume /
  partial / custom field, var_add create-or-increment + non-numeric reject,
  var_move deepcopy isolation between entities), status dispel/transfer (tag
  dispel, max cap, consume-on-reject to an immune dest), transform/foreach/table/
  reveal_fog smoke on a multi-tile board, reveal_fog serialization + footprint
  `around`, the dice `kh0` guard (survived the roll_detail refactor), the inline
  `$()` classification (drift guard + validate_arg_safe reject path) and the
  raw_args opt-out set (ent/foreach/batch/run/macro/eval — exactly the meta
  commands), the ATB selection math + death-mid-turn robustness, and the Discord
  board/approval logic (pass-18's approval-match fix holds). Two real bugs in the
  MACRO control-flow interpreter (both let a macro hang/crash the bot, defeating
  the stated runaway backstop):
  - **Nested `repeat` over an empty body bypassed macro_step_limit (HIGH).**
    `_exec_macro` charged the step budget ONLY on `cmd` nodes, so a `repeat`
    whose body emits no command (an empty or directive-only block) never touched
    the budget. `repeat 1000 / repeat 1000 / end / end` ran 1e6 iterations (0.4s)
    and a third nesting level (1e9) would hang the bot for minutes — exactly the
    runaway macro_step_limit exists to stop. Fixed by charging one budget unit
    PER `repeat` ITERATION (in the `for k in range(...)` loop), so total loop
    work across all nesting is bounded by the limit regardless of body contents.
    macro_step_limit's meaning is now "dispatches PLUS loop iterations"; its desc
    updated. Tight legitimate loops are unaffected (default budget 10000).
  - **Recursive macros overflowed the Python stack (HIGH → 💥).** A macro line
    can `!macro run <other>`, and each `!macro run` allocated its OWN fresh step
    budget — so a self-recursive macro (`loop` = `macro run loop`) or a mutually-
    recursive pair recursed until `maximum recursion depth exceeded` (a caught
    but ugly 💥, and a lot of wasted work). Actions guard this with a recursion
    limit; macros had none. Fixed with a new `macro_recursion_limit` rule
    (default 20) + a `MatchManager._macro_depth` counter (on the manager = the
    dispatch stack, so it accumulates even if a macro line switches the active
    match), incremented around each `!macro run`'s `_exec_macro` and restored in
    `finally`. Beyond the limit the run aborts with a clean ❌, not a 💥. Legit
    nesting (`top`→`mid`→`leaf`, 3 deep) and 30 SEQUENTIAL inner runs both still
    work (depth returns to base between sequential cmds; resets between top-level
    runs). NOTE for future sweeps: `!batch` CANNOT recurse this way (its
    subcommands are literal text, no named indirection), but `!run <file>` on a
    self-referencing file has the same unbounded-recursion shape — left as-is
    (host-only disk I/O, far more exotic than a stored player-triggerable macro).

- **Audit-pass-19 (external-report triage): summon regression + var_get default
  + harness hardening (scenario 554).** A second Opus instance found two bugs
  plus a harness blindspot; all verified and fixed:
  - **The summon system was COMPLETELY BROKEN (HIGH, FIXED).** Every
    `summon` / `summon_near` / `summon_from` raised `❌ Runtime error: 'x'` and
    created NOTHING. `Match.summon_entity` called `Entity.from_dict(d)` BEFORE
    seeding `d["x"]/d["y"]` (they're set later from the resolved placement), but
    a template has its position STRIPPED and `from_dict` does `int(data["x"])`
    (subscript, no default) → `KeyError('x')`. Fixed: `d.setdefault("x", x)` /
    `setdefault("y", y)` before the from_dict probe-build (they're overwritten
    with place_x/place_y anyway). This had rotted undetected across ELEVEN of
    its own scenarios (317/318/321-324/336/340/351 + the summon-in-passive 319
    and summon-tile 320) because the harness only flagged 💥, not a top-level ❌.
  - **`var_get` rejected a default arg (LOW-MED, FIXED).** `var_get(eid, path)`
    was 2-arg, but the obvious create-or-read idiom `var_get('h','alarms',0)+1`
    (used by scenario 427's watcher effect, and mirroring `corpse_var`'s
    `default`) errored "takes 2 positional arguments but 3 were given". Added an
    optional `default`: returned on a missing path, still raises without one.
  - **Harness blindspot hardened (the meta-fix).** `run_scenarios.py` only
    flagged 💥 / "Syntax error", so a top-level `❌ Runtime error:` /
    `❌ Unexpected error:` (a core feature silently failing) slid through — the
    exact trap that hid the summon regression. Added `_ERROR_MARKERS` flagged
    UNLESS a scenario opts out with a `HARNESS-ALLOWS-ERRORS` tag in its
    Expected prose (for the few scenarios — 222 func-deletion, 551 vital-write
    rejection, 554 var_get-no-default — that DELIBERATELY surface a top-level
    error). 💥 / Syntax always flag regardless. So the whole suite now passing
    (551/551) is a real signal that no core feature is silently ❌-ing.

- **Audit-pass-18 (external-report triage): four fixes (scenarios 551-553).**
  A second Opus instance was asked to find flaws; all four it reported were
  verified against the code (repro'd where harness-reachable) and fixed:
  - **Vital-var WRITE corruption (MED, FIXED).** Any write path could set
    hp/max_hp/initiative to a non-number or nest under it (`!ent set_var h hp
    abc`, `var_set('h','hp','boom')`, `hp.x`, `var_add('h','hp.inner',5)`),
    after which the `int()`-based hp getter 💥'd `!list`/damage/is_alive — and it
    round-tripped through save/load. The DELETE path was already guarded
    (remove_var, pass-16); the symmetric WRITE path wasn't. Fixed at the
    `Entity.write_var` chokepoint: a write whose top-level key is a protected
    vital var is coerced to int (numeric strings/floats OK) or REJECTED
    (non-numeric / bool), and NESTING under a vital (`hp.x`) is refused
    (`_coerce_vital_value` helper). Skipped pre-bind (no match).
  - **`!ent clone` ignored parts/mounts (MED, FIXED).** Clone did `src.to_dict()
    → from_dict → spawn`, so it DROPPED a multi-part creature's limbs and copied
    protected relational fields raw — a mounted clone inherited `mounted_on`/
    `mount_slot` (a phantom rider bypassing slot capacity / on_mounted), a part
    clone inherited `part_of` (a phantom limb on the original's parent). Fixed to
    mirror `MatchManager.copy_entity`, same-match: clone the whole part SUBTREE
    (parents first, ids/`part_of`/`__follows` remapped, located-part offsets
    kept, `_restamp_parts_for`), STRIP `mounted_on`/`mount_slot`, and REFUSE
    cloning a body part ("clone its parent instead"). Gotcha fixed mid-work: the
    root must take the planned id, so `taken` excludes the plan's cid.
  - **Unguarded `int()` in `!ent hp/init/add/tp` (LOW, FIXED).** A non-numeric
    numeric arg (`!ent hp foe abc`) 💥'd pre-mutation where a clean ❌ belongs
    (sibling verbs already wrapped int()). Wrapped the four sites in try/except
    ValueError → ❌.
  - **Discord approval resolved against the wrong match (LOW, Discord-only,
    FIXED by inspection).** `_ApprovalView._match` popped/ran the request via the
    channel's ACTIVE match, but request ids are per-match sequential (r1, r2…),
    so a host switching matches mid-approval could pop a DIFFERENT match's
    same-id request. Fixed: `add_pending_request` now stores `match_id` on the
    request (runtime-only, harness-verified present); `_match` resolves against
    it, and the approve re-dispatch points the channel at the request's match
    before running (falls back to active for legacy requests).

- **Audit-pass-17 (hands-on): CLEAN PASS — no bug found.** A by-hand
  interaction sweep over the recent-feature cross-products (graphics + fog/POV +
  mounts + status + transform + containers + macros), all exact, no code change.
  Verified (so future passes can skip re-grinding these):
  1. **Serialization idempotency** — `to_dict == to_dict(from_dict(to_dict))` on
     a match exercising EVERY recent serialized field at once (named tables,
     fog_reveals, border_show/color/opacity, render_mode, background, status-def
     overlay sprites, the overlays var, mounts, parts, container vars, control-
     flow macros, watchers, anchored zones, team data, sprite_layer var). No
     missing/dropped field.
  2. **`render_scene` POV/fog/disguise consistency** — for every POV the emitted
     entity + tile placements match the SAME `entity_visible_to` /
     `tile_visible_to` + `_fog_terrain_visible` predicates the ASCII path uses
     (no graphics-side info leak); a disguised entity shows the decoy name to
     enemies, the real name omniscient.
  3. **Status cluster** — dispel by `tag:` removes all matching + keeps others;
     transfer to an IMMUNE dest is consume-on-reject (gone from source, doesn't
     stick); counter to 0 on a CUSTOM field auto-removes.
  4. **Overlays render on a visible mounted rider** (the `_emit_entity_placement`
     overlay pass covers riders/region parts); item_consume inside a macro
     `repeat` decrements per iteration.
  5. **transform/revert** — id/pos/team preserved, statblock wholesale-swapped
     (old inventory dropped), revert restores it; needs a CAPTURED statblock
     (`store_entity_into_var` / a live id), NOT a flat hand-built dict.
  6. **Render resolution precedence** — per-tile `border_color` data >
     per-match border field > rule; per-tile `sprite_layer` data overrides the
     z-layer rule; inline `$()` negative result works in `!foreach`.
  7. **Macro control flow** — nested `if`/`repeat` + `$#` index, `macro_repeat_limit`
     clamp, `macro_step_limit` runaway abort, unbalanced-block rejected at `set`.
  Process note for future harness authors: two "failures" this pass were BOTH
  test errors — spawning a second entity ON a vehicle's occupied cell (the add
  silently fails, so the later mount has no rider), and using a FLAT transform
  template instead of a captured statblock (apply_statblock finds no `vars` →
  max_hp None). Neither is an engine defect.

- **PROCESS RULE — args / formula functions (the user's standing directive).**
  Inline `$()` args evaluate formula functions, so the read-vs-write
  classification is a SAFETY boundary, not a nicety. WHENEVER you touch formula
  functions or any feature that evaluates user formulas in args:
  1. Every new `_MATCH_FUNC_NAMES` entry MUST be classified as
     `ARG_SAFE_MATCH_FUNCS` (pure read/calc — anything that gets / calculates /
     evaluates) or `ARG_MUTATING_MATCH_FUNCS` (anything that changes game state
     — STRICTLY banned from args). The module-load drift guard in `formula.py`
     fails the build if you forget, but DECIDE deliberately — a mis-classified
     mutating function in args is a disaster (a player could `$(kill(boss))`).
     When unsure, classify as MUTATING (default-deny).
  2. Add `$()` arg scenarios — at minimum a read-only happy path AND a
     security case proving the new/changed mutating functions are rejected and
     do NOT mutate (see scenarios 541-543 for the pattern).
  (scenarios 507-511).** A seventh sweep (three read-only survey agents across
  status/passive/event, movement/geometry/LOS, action/choice/dispatch/clamp;
  every candidate verified in code — and most behaviorally repro'd — before
  fixing). Five real bugs + two user design calls:
  - **Load-side shallow-copy (HIGH).** The SAVE side was deepcopied in passes
    5/6 (`Entity.to_dict` vars, `Match.to_dict` tiles, `_zone_to_dict`), but the
    LOAD side still re-shared nested data with the RETAINED snapshot:
    `Entity.from_dict` did `vars=dict(...)`, `_coerce_status_dict` did `dict(v)`
    per status, and `Match.from_dict` reused each tile dict by reference
    (`m.tiles[(x,y)] = val`). Since `from_dict` runs on `!history restore` /
    undo / action rollback (the snapshot stays in history), a later in-place
    `!ent set_var inv.x` / `!tile set` corrupted the saved snapshot, so a second
    restore returned the mutated value. Fixed all three with `copy.deepcopy`
    (zones' `_zone_from_dict` already deepcopied `data` + rebuilt `hooks`, so it
    was already safe). Scenarios 507-508.
  - **Ghost passives (MED).** In `fire_status_event`, `fire_hook`, and
    `emit_event`, global/team handlers fire BEFORE the entity's own handlers. If
    a global/team handler removed the entity (kill/remove — in a status hook the
    affected entity is bound as `self`, NOT `target`), the own-handler loop still
    ran on the just-removed entity, firing side effects from beyond the grave.
    Added an existence re-check (`id in self.entities`) before each own-handler
    loop, mirroring the loops' existing top-of-iteration guard. Scenarios
    510-511.
  - **`!status apply` crash when a hook removes the target (MED, pre-existing).**
    The command handler did `e = m.entities[eid]` right after `apply_status`,
    KeyError'ing if a lifecycle hook (e.g. an on_status_added passive that kills
    the entity) removed it mid-apply. Now reports "Applied ... which was then
    removed by a triggered effect" instead of crashing. Surfaced by scenario 510.
  - **max_level hard ceiling (user call → cap everywhere).** `max_level` capped
    only the `add_level` stacking mode; a FIRST application or a `replace` with
    an explicit level above max was uncapped (`!status apply h burn 10` on a
    fresh max_level=3 → level 10). User's call: make max_level a hard ceiling on
    the level field EVERYWHERE. New `Match._cap_status_level(sdef, lvl)` helper
    applied at all three apply sites (first / replace / add_level). Scenario 509.
  - **LOS-on-opaque (user call → KEEP current, no code change).** For the
    sight-aware line queries (`entities_on_los`, `entities_in_line_until`), an
    entity standing ON the FIRST opaque cell (e.g. an enemy at the near edge of
    smoke) is RETURNED as visible/hittable; only entities BEYOND the opaque cell
    are cut. CONFIRMED INTENDED (consistent with `has_los`'s "the target's own
    opacity never blocks" convention — you can see/shoot something at the wall
    surface, not past it). Documented here so the ambiguity doesn't recur; do
    NOT "fix" it to exclude the on-opaque entity.
  - Re-verified correct (no change): choice-replay RNG snapshot/restore +
    summon-budget reset + buffer reset, the `action._rollback_match` runtime-
    field list (incl. event-stack), clamp/death-check ordering in `write_var`,
    the dispatch gate, status counter auto-removal + cross-status removes/
    blocked_by, resistance mode-awareness + the force path, attached-part tick
    sharing, and (re-confirmed footprint-correct) all vision/LOS casts, distance
    gaps, and movement validation.

- **Audit-pass-8 fix: ghost passives in the VAR-hook firing paths (scenario
  512).** An eighth sweep (three read-only survey agents across zones/clamps/
  tiles/aliases, death/corpse/parts/segments, and action/choice/formula/
  dispatch). Agents 1-2 found their subsystems correct (zones' anchored auras +
  footprint interaction, clamp ordering, tile precedence, alias resolution; the
  whole death/corpse/parts/segment/mount cluster incl. the part-destroy latch +
  revive subtree + mount-strip — all re-confirmed sound). Agent 3 found the ONE
  real bug: pass-7 guarded the ghost-passive case (a global/team handler removes
  the entity, then its OWN handlers must not fire from beyond the grave) in
  `fire_status_event` / `fire_hook` / `emit_event`, but MISSED the var-hook
  firing paths. `_fire_var_event_inner` (wave 1 = exact `on_var_{kind}`, wave 2
  = `on_var_written` catch-all) and `_fire_var_attempt_inner`
  (`on_var_write_attempt`) each fire global/team handlers then the entity's own
  passives WITHOUT re-checking the entity still exists. So a global var-hook that
  removes the affected entity left the own-passive loops iterating a stale `e`.
  Fixed with the same `if entity_id in self.entities:` guard before each own loop
  (three sites). NOTE for repro authors: in a VAR hook `self`/`this` =
  current_entity_id() (the active-turn entity), NOT the affected entity, and
  `target` is NOT bound (var-event extras expose changed_key/old_value/
  new_value/hook_name/intended_value/was_clamped only) — reference the affected
  entity by literal id or via `self` only when it IS the active entity. (This
  differs from STATUS hooks, where the affected entity is bound as `self`.)

- **Audit-pass-9 fix: snake-trail coords shift with resize + cross-match copy
  (scenarios 513-514).** A ninth sweep — this one INTERACTION-focused (single
  subsystems are heavily swept now). Three read-only survey agents: (1)
  visibility/render/disguise × transform/mount/viewport/legend/fog, (2)
  modifier/status/team × transform/death/parts, (3) resize/transfer/choice-
  replay/undo. Agents 1-2 re-confirmed their interaction surfaces correct
  (disguise POV gating vs fog vs mechanics separation; viewport/legend window
  clipping; team-membership-change reads `e.team` fresh for modifiers+passives;
  status-removal drops modifiers live; transform wholesale-replaces status;
  part-on-different-team gets its own team passives; choice-replay preserves the
  event stack + resets summon budget; watchers fire only at the top-level
  command boundary, never mid-action). Agent 3 found the one real bug:
  - **`__seg_path` / `__seg_last` (the engine-managed snake-trail coordinate
    vars) were not shifted by `resize_grid` nor offset by `copy_entity`.** These
    are the ONLY coordinate-bearing ENTITY VARS the engine owns (head vars: a
    list of `[x,y]` cells the head has occupied + the head's last cell, used by
    `path`-follow-mode segments). resize_grid shifted entities/tiles/zones/
    explored/channel_views but missed them, and copy_entity remapped part_of +
    `__follows` but didn't offset them — so a path-mode snake re-laid its body at
    STALE cells after a center/edge-anchored resize, and a transferred snake
    re-laid at the SOURCE's coordinates in the destination match. Fixed with a
    shared `Match._shift_snake_path_vars(vars, ox, oy)` static helper called from
    the resize entity-shift loop and per-spawned-entity in copy_entity (delta =
    the same offset the anchor moves by). NOTE: copy_entity is a `MatchManager`
    method, so it calls the helper as `Match._shift_snake_path_vars(...)`, not
    `self.` (a transfer scenario now also guards that cross-match path against
    a crash). KNOWN pre-existing quirk surfaced (NOT fixed — separate from the
    coord bug): path-mode segments legitimately OVERLAP early (the trail is
    shorter than `(segments+1)*spacing`), and copy_entity re-validates occupancy
    on spawn, so transferring a snake whose trail hasn't spread yet fails with
    "cell occupied" — left as-is since overlapping located parts are themselves
    a questionable state.

- **Audit-pass-10 fixes: turn-order skip-loop + mount push/pull footprint
  (scenarios 515-517).** The widest sweep yet — FIVE read-only interaction
  agents (mounts/vehicles deep; movement/block/opacity; turn-order & clocks;
  formula-sandbox safety; access-gate/dispatch) PLUS hand-written numeric
  assertion harnesses for the gnarliest primitives. Agents confirmed correct
  (no change): the whole movement/block/opacity surface (footprint-aware
  push/pull/swap/group, fail-open conditions, LOS symmetry/corner modes,
  raycast/first_opaque endpoints), the formula SANDBOX (no escapes — empty
  `__builtins__`, entity[X] mandatory-`.path`, every HOOK_CONTEXT name in
  `_who_arg`'s dynamic branch, `normalize_body_source` at every body boundary,
  kh0/explode/band/roll_table edges), and the ACCESS GATE (no batch/run/macro/
  foreach/alias/cmd bypass; overrides-before-rule precedence; approval re-gates
  with the approver's authority). My own numeric harnesses re-verified
  `damage_part` (every cap mode × percent × rounding × 0/0 passthrough × vital)
  and the `apply_modifiers` fold (add/inc%/more%/set/min/max tiers, priority
  bumps, op-order, stat caps) — all exact. Three real bugs fixed:
  - **Turn-order crash when a skip-status round-wrap empties the order
    (HIGH).** pass-3 guarded next_turn against an emptied order after the
    turn_end hooks and after `_advance_index`'s round-wrap, but NOT after
    `_skip_to_eligible` — whose OWN internal `_advance_index` (stepping over a
    `skips_turn` entity) can wrap and fire on_round_end/start hooks that remove
    the last entity. next_turn then did `turn_order[active_index]` on an empty
    list → IndexError (💥). Fixed: re-check `if not self.turn_order: return
    (None, log)` after BOTH `_skip_to_eligible` calls (opening-round + normal
    paths). Scenario 515.
  - **Skip-loop stale bound inflates round_number (MED).**
    `_skip_to_eligible` sampled `n = len(turn_order)` ONCE; if a round-wrap hook
    SHRANK the order mid-skip, the stale `n` let the loop keep cycling the
    survivors, firing on_round_end repeatedly and inflating round_number (a
    command-only repro: 3 entities, two removed on round_end, advanced round by
    3 instead of 1). Fixed: bound by the CURRENT order size each step
    (`if checked >= len(self.turn_order): return False`), keeping the initial
    `n` only as a hard cap against a skip-hook that GROWS the order. Identical
    behavior in the common no-shrink case. Scenario 516.
  - **push/pull validated the RIDER's footprint, not the vehicle's (HIGH).**
    `push_entity`/`pull_entity` walked the legal-prefix using the target
    entity's footprint, then committed via `e.move_dirs`, which redirects a
    mounted DRIVER to its vehicle (`_mount_move_redirect`). So pushing a 1×1
    pilot of a 2×2 tank validated the pilot's 1×1 path (which sat inside the
    tank's own cells → read as blocked → silent no-op) or, on a clear lane,
    committed the vehicle move that the prefix never validated (stop-early /
    mid-commit mismatch). Fixed: resolve `_mount_move_redirect()` at the START
    of push/pull (after the `n<=0` guard), so the VEHICLE's footprint is what's
    validated AND committed — and a non-driver PASSENGER raises "dismount
    first." Mirrors the tp/move_dirs/swap redirect (single-level, like swap).
    Scenario 517. (Process note: while reverting this for a pre-fix check I
    re-inserted the block at the wrong `dx,dy = DIRECTION_VECTORS[canon]`
    occurrence — that string also appears in `move_dirs` — briefly corrupting
    move_dirs. Lesson: a bare `s.find(old)` restore is unsafe when `old` isn't
    unique; prefer the Edit tool with surrounding context.)

- **Audit-pass-11 fix: damage_spread fragment-mode crash + serialization
  consistency (scenario 518).** An eleventh sweep — three read-only interaction
  agents (serialization round-trip completeness; corpse/aura/time-hooks;
  status-tick/watchers/event-bus) PLUS hand-written numeric assertion harnesses
  for the primitives that still lacked one (`damage_spread`, the clamp
  chokepoint). The agents found NO confirmed correctness bugs (their flagged
  serialization items were FALSE POSITIVES — see below), but my numeric harness
  caught the real one:
  - **`damage_spread` fragment mode crashed without a `random_seed` (HIGH).**
    The `fragment` branch did `rng = getattr(self, "_rng", None) or random`, but
    `logic.py` never imported `random` (only `formula.py` did, for its
    `_active_rng`). `Match._rng` is None by default and only built when a
    `random_seed` is configured AND a formula roll initializes it — so under the
    DEFAULT (no-seed) config every `damage_spread(target, total, "fragment")`
    hit the `or random` fallback → `NameError: name 'random' is not defined` (a
    `❌ Runtime error` through the action/formula path). The weighted / uniform /
    main_only modes use no RNG, which is why scenarios 410-413 never caught it.
    Fix: `import random` at the top of logic.py (the fallback now mirrors
    formula's `_active_rng` exactly: seeded `_rng` when present, global `random`
    otherwise). Scenario 518.
  - **Serialization deepcopy consistency (NOT a live bug — defensive).**
    `Match.from_dict` restored `watchers` and `bound_channels` with a shallow
    `dict(v)` while `to_dict` deepcopied them. Two survey agents flagged this as
    the pass-5/6/7 load-side corruption class, but VERIFICATION showed it's a
    FALSE POSITIVE for correctness: both hold FLAT scalar dicts (`watchers`:
    condition/effect strings + bool `last` + bool `once`; `bound_channels`
    meta: `label`/`pov` strings), and the only mutations (`w["last"] = now`,
    `meta["pov"] = ...`) are TOP-LEVEL key reassignments on the already-
    independent `dict(v)` copy — they never reach the retained snapshot (which
    needs a NESTED in-place mutation to corrupt, as `vars`/tiles/zones had).
    Still, switched both to `copy.deepcopy(v)` for symmetry with the save side +
    every other dict field, so the inconsistency stops magnetizing audit
    re-investigation and a future nested field can't silently reintroduce the
    bug. (Documented as verified-safe so pass N+1 doesn't re-flag it.)
  - Numeric harnesses re-verified EXACT (no bugs): `damage_part` (every cap mode
    × percent × rounding × 0/0 passthrough × vital), the `apply_modifiers` fold
    (add/inc%/more%/set/min/max tiers, priority bumps, op-order, stat caps),
    `damage_spread` apportionment (largest-remainder shares sum to total across
    weighted/uniform/fragment/main_only/spatial-miss/all-zero-weights), and the
    CLAMP chokepoint (hard always clamps; soft engages only crossing from the
    legal side and stays DORMANT past the bound; max-before-min ordering).
  - OPEN QUESTION (RAISED → RESOLVED, shipped as the `suspend` mode below): a
    non-vital body PART destroyed by damage (hp→0 via `damage_part`) LINGERS
    attached-but-dead and does NOT route through `Entity.remove`, so its anchored
    AURA was never released. While investigating, established the current
    death-aura behavior: entity death/kill/despawn (and the part-death cascade)
    all route through `Entity.remove` → `_release_anchored_zones` → the
    `anchored_zone_on_anchor_loss` rule (delete/freeze); the ONE gap was the
    lingering-destroyed-limb case. The user's call: add a third mode that
    SUSPENDS the aura while the anchor is dead and RESUMES it on revive/heal,
    for BOTH entity death/revive and part destroy/heal. Shipped — see next entry.

- **Anchored-aura `suspend` mode (suspend-while-dead, resume-on-revive) —
  SHIPPED (scenarios 519-520).** A third value for the
  `anchored_zone_on_anchor_loss` rule, alongside `delete` (default) and
  `freeze`: `suspend` clears the aura's CELLS (it goes inert — no render, no
  hooks, no membership) but KEEPS the anchor binding, so the aura automatically
  RE-STAMPS around the anchor if that entity is revived from its corpse or that
  part is healed above 0. Implementation: `_release_anchored_zones` gained the
  `suspend` branch (`z["cells"] = set()`, binding retained); the symmetric
  `_resume_anchored_zones(eid)` = `_restamp_anchors_for` (a no-op unless the
  anchor is alive again, since `_stamp_anchored_zone` only re-fills for a live
  anchor). Wiring: (1) `_process_part_death` now calls `_release_anchored_zones`
  on the destroyed limb (this ALSO closes the original gap for delete/freeze —
  a destroyed limb's aura now follows the rule like any other anchor loss,
  whereas before it was left stale); (2) `damage_part`'s heal path (latch clear
  on hp>0) calls `_resume_anchored_zones`; (3) `revive_corpse` resumes the
  entity + its whole part subtree AFTER the revive effects + check_death settle
  (resuming only if the entity is actually `is_alive` — a revive policy that
  leaves it dead keeps the aura suspended). Multi-tile anchors work (the
  footprint disc re-stamps on resume). Suspended auras serialize for free (a
  zone with empty cells + an anchor binding). CAVEAT (documented in the rule
  desc): a true despawn (`!ent remove`) under `suspend` leaves an inert bound
  zone that can't resume (nothing to revive) — re-anchor or delete it manually.
  Default stays `delete` (backward compat); `delete`/`freeze` unchanged.

- **Audit-pass-12 (stability sweep, HANDS-ON): FormulaError-shadowing crash
  fixed (scenario 521).** First pass done entirely by hand per the standing
  directive (no survey-agent swarm) — reading subsystems + writing numeric/
  property assertion harnesses. Verified CORRECT with harnesses (no change):
  `side_hit`/`hit_location` geometry (4-way all facings × cardinals, 8-way
  corner detection, the 1×1 box==center invariant over 192 source/facing
  combos), `has_los` SYMMETRY (property test, ~7k cell-pairs × permissive/
  strict/open, 0 asymmetric), `raycast` straight-line, the serialization
  round-trip (idempotent `to_dict==to_dict(from_dict(to_dict))` on a complex
  match — multi-tile + parts + path-snake + mount + suspended aura + statuses
  + team + watchers + macros + fog + disguise — AND load-side deepcopy holds),
  `band()` boundaries (ranges / `n` / `lo+` / `-hi`), and dice (`kh`/`kl`/
  explode in range). One real bug fixed:
  - **`FormulaError` UnboundLocalError in `status_cmd` (HIGH, user-facing).**
    `vtt_commands.py` imports `FormulaError` at module level (line 23), but
    `status_cmd` had a REDUNDANT function-local `from formula import
    FormulaEngine, EvalCtx, FormulaError` deep in the handler (the counter
    path). That makes `FormulaError` a function-LOCAL for the WHOLE function,
    so the EARLIER `except FormulaError` in the `!status tick` validation path
    raised `UnboundLocalError: cannot access local variable 'FormulaError'`
    instead of the intended `❌ Invalid tick formula: ...`. So setting ANY
    invalid status-tick formula (typo / unknown identifier / bad syntax)
    crashed with a `💥`. Fixed by deleting the redundant local import; also
    hoisted `validate_formula` into the module-level import and removed the
    same redundant-local pattern from the tile-hook / zone-hook / status-tick
    handlers (they were latent versions of the same shadowing class). The
    remaining `from formula import ... as _FE/_vp/_FEng` aliased locals are
    SAFE (an alias doesn't shadow the module name). NOTE confirmed while here:
    a status-tick formula reads the instance level via `status_get(self,
    status_name, 'level')` — `level` is NOT a bare binding (only `status_name`
    is in the tick EvalCtx extras), so the validator correctly rejects a bare
    `level` (CLAUDE.md's earlier `5*level` shorthand was illustrative).
  - OPEN QUESTION raised with the user (LOS corner-mode consistency) → RESOLVED
    (corner-aware, shipped): `has_los` applied the `los_corner_mode` flanker
    check at diagonal crossings, but `first_opaque` / `raycast` walked the same
    thin `_line_cells` path WITHOUT it — so they only agreed in `open` mode.
    With an opaque corner-X (both flankers opaque), `has_los`=False (blocked)
    while `first_opaque`=None and `raycast` reported the beam reaching the
    target. The user's call: make first_opaque/raycast corner-aware so sight
    and beams agree. Fix: factored the corner-aware walk into the single shared
    `Match._los_stop(viewer, x1,y1,x2,y2)` → `(last_clear, blocked, blocker)`,
    and reimplemented all three over it — `has_los` = `not _los_stop(...)[1]`,
    `raycast` = `last_clear` (stops at the pre-corner cell on a corner-X block),
    `first_opaque` = the on-path `blocker` if any, else `last_clear` on a corner
    block, else None. One walk = one source of truth, so the three can never
    drift on the corner rule again. `_line_cells` stays for the geometry-only
    consumers (`entities_in_line_ignorelos` = walls-ignored by design;
    `entities_on_los` was already corner-aware via per-cell `has_los`).
    Verified: property sweep across permissive/strict/open — `first_opaque`
    None ⇔ `has_los` clear and `raycast`==target ⇔ clear, 0 mismatches; the
    has_los refactor left the symmetry property + full regression intact.
    Scenario 522.

- **Audit-pass-13 (hands-on): ghost STATUS-TICK guard (scenario 523).** Another
  by-hand pass — numeric/behavioral harnesses, no agent swarm. Verified CORRECT
  with harnesses (no change), broadening the "primitives are exact" coverage:
  the AoE/area enumerators (`entities_in_cone`/`_rect`/`_area`/`_within`,
  `nearest_entity`, `chain_targets` — incl. footprint nearest-cell distance and
  the chain starting from the nearest to the origin, NOT including the origin),
  `transform` hp modes (percent/keep/full) + revert fidelity, shield/absorb
  (`absorb_damage`/`shield_total` — priority order, tag matching, penetration),
  the whole fog/vision stack (range, multi-tile sight UNION, fog_los blocking,
  fog_memory `full` vs `terrain` at remembered cells), status resistance
  (source-gating equipped-vs-inventory, sum/max/first stack, immunity, applied-
  level reduction, full-resist no-op, `force` bypass), and an edge/crash probe
  across dice / `roll_table` / `band` / coord extractors (every invalid input
  is a clean FormulaError, never a 💥), plus event-bus nested-payload integrity
  and the choice-replay exactly-once invariant (a side effect before two
  `choose()`s applies once net despite the rollback+replay per choice). One real
  bug fixed:
  - **Ghost status tick after a lethal tick (MED, the missed ghost-firing
    site).** `fire_status_tick` snapshots an entity's status NAMES so
    status-removal mid-tick doesn't break iteration, but it never re-checked the
    ENTITY still existed. So if status A's tick kills/removes the entity (a
    lethal DoT — or a part tick routing `damage_part` to a vital parent), its
    remaining statuses B, C, … still ticked "from beyond the grave": a tick
    writing to ANOTHER entity ghost-applied (e.g. a dead unit's aura still
    damaging others), and one reading `entity[self]` logged a spurious
    `⚠️ status_tick FAILED: Entity '<id>' not found` (caught, no crash). This is
    the same invariant the passes-7/8 ghost-passive guards enforce for the hook
    / event / var-hook firing sites; the status-TICK site was the one missed.
    Fix: `if eid not in self.entities: break` at the top of the per-status loop
    (after the name snapshot). A non-lethal multi-status tick still fires every
    status; only an actually-removed entity stops. Scenario 523.

- **Audit-pass-14 (hands-on): FIRST CLEAN PASS — no bug found.** Continued the
  by-hand discipline (numeric/behavioral harnesses, no agent swarm). Hand-
  verified TEN subsystems/interaction-combos against assertion harnesses, ALL
  exact — no code change. This is the first pass that surfaced zero defects, a
  signal the harness-testable engine core is solid in these zones. Verified
  (so future passes can skip re-grinding these):
  1. **Multi-tile push/pull/swap geometry** — a 2×2 push stops exactly at the
     cell before a wall; push-to-edge clamps the anchor; swap of a 2×2 with a
     1×1 exchanges anchors; pull stops adjacent to a 2-wide body.
  2. **Segment sever modes** — `cascade` removes the cut + everything behind;
     `split` promotes the segment behind to a new independent head (cleared
     part/segment linkage, re-parented tail, stamped split-head template, added
     to turn order).
  3. **Macro/foreach substitution** — `$10`/`$11` parse as the 10th/11th arg
     (not `$1`+"0"), `$@` expands to all args, missing `$5`→"", no
     re-expansion of a `$2` appearing inside an arg value, foreach `$id`/`$name`.
  4. **Mount slot math** — capacity budget, per-rider `cost` formula, `condition`
     gate, re-seat doesn't self-block, mount-cycle guard.
  5. **Region parts** — facing-aware footprint-region projection (front/back/
     left/right/corners/center) rotates correctly with the parent's facing on a
     3×3.
  6. **resize_grid coordinate shift** — corpses-in-tile-data, anchored auras
     (re-stamped around the shifted anchor), mounts (vehicle + carried rider),
     and a subsequent revive all land at the shifted cells; no crash.
  7. **Recursion/limit guards** — event-bus re-emit bounded at
     `event_recursion_limit` (64), var-hook self-write bounded by the
     `_var_event_depth` guard, self-referential action bounded at the recursion
     limit (8) with a clean error — no hangs/crashes.
  8. **Multi-feature combos** — transforming into a larger footprint re-stamps
     the anchored aura's disc bigger (and revert shrinks it back); pushing a
     multi-tile vehicle carries its rider AND re-stamps its aura together.
  9. **Watchers** — edge-trigger (false→true fires once, no re-fire while true,
     re-fires after the condition resets), `once` removal, `last` serialization.
  10. **Corpse introspection** — `corpse_var`/`corpse_has` (nested paths +
      default), `corpse_status_has`/`get`/`names`, large-corpse `corpse_cells`
      footprint, and revive restoring the footprint.
  Note: numeric primitives (damage_part, modifier fold, damage_spread, clamps,
  side_hit, LOS/raycast, dice/band/roll_table, shields, status resistance, fog/
  vision) were already harness-verified exact in passes 11-13. With this pass,
  the harness-testable core is broadly covered; the likeliest remaining defect
  surface is the Discord adapter (not harness-testable) and genuinely new code.

- **Audit-pass-15 (hands-on): SECOND CLEAN PASS — no bug found.** Swept the
  remaining untouched-by-hand areas; all exact, no code change. Verified:
  status interaction cluster (tags, cross-status `removes`/`blocked_by` by
  bare-name + `tag:` tokens, counters auto-removing at <=0 on `duration` AND
  custom fields); team-level state (resources `team_get`/`set`/`add` dotted,
  team `modifiers` aggregated per member with a non-member excluded, team
  passives firing on the acting member, membership-change picks up the new
  team's modifiers); tile precedence (instance `glyph`/`block`/`opaque` >
  template > rule) + tile time-hooks firing per placed instance; alias
  resolution (expands before the gate); batch undo grouping (a `!batch` reverts
  as ONE history entry) + single-command + tp undo; `!find` predicates
  (var compare, `team=`, `hp<`, `near:<id>:<r>`, `within:<x>:<y>:<r>` — all
  footprint/Chebyshev-correct). PROCESS NOTE for future harness authors: two
  "failures" this pass were BOTH test-harness errors, not engine bugs — (1)
  there is NO `ent damage` subcommand (damage is `ent hp <id> <-n>`), and (2)
  `restore_snapshot` (undo/history restore) REPLACES the Match object in
  `mgr.matches[mid]`, so a captured `m = mgr.matches[id]` reference goes STALE
  after an undo — always RE-FETCH `mgr.matches[id]` after a restore/undo or
  you'll read the pre-undo object and think undo is broken. Two clean passes
  (14-15) in a row → the harness-testable engine core is solid.

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
