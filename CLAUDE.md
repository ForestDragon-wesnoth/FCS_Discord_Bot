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

Recently shipped or in-flight:
- Match history / undo system
- Passives + var hooks + tile hooks
- Status system (entity.status dicts; tick formula configurable)
- Clamp system (entity + system-level)
- Tile templates + tile time-hooks
- Formula functions (`!func`)
- Alias system (per-match + per-system)
- `!batch` / `!run` for grouped commands under one undo entry
- `!history diff` between snapshots
- `!find` with predicate prefixes (status:, group:, action:)
- Push/swap/step movement primitives + `on_entity_step` hook
- Action system (full body language with cmd/fail/source/target/args,
  transactional rollback, target types entity/location/entity_list/
  location_list/none/corpse/corpse_list, recursion limit, allowlist,
  on_action_used/on_action_used_on_target/on_action_failed, `kill`/
  `revive`/`has_action`/`use_action`/etc.)
- Container var primitives (var_keys, var_sum, var_clear, etc.)
- Summon system (entity templates in vars/tiles; summon/summon_near/
  summon_from/entity_snapshot/remove_entity)
- round_number / turn_index match-clock primitives
- Death and corpses (this PR — #34, currently open):
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
    (channel_key -> {"label"?}), uncapped. `!match bind/unbind/channels`;
    `match use`/`bind` keep `MatchManager.active_by_channel` in sync. The
    `label` is reserved for the NOT-YET-BUILT per-team / fog-of-war
    views — binding routing exists, per-channel rendering does not.
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
  Being built gradually; this is the first slice. Full LOS/range fog is
  explicitly later.
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
    map. Tiles, zones, and CORPSES are NOT yet POV-filtered (Piece 3).
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

What the user has flagged as next-on-their-mind:
- The user repeatedly chooses the "more gamerules, fewer hardcodes"
  direction. Almost every behavior the engine performs should be
  configurable.
- **`default_entity_vars` gamerule (SHIPPED — fog precursor).** Dict rule
  (var-path -> default value) applied in `Entity.spawn` at the very start,
  before vital-var validation, filling only MISSING vars (so an `!ent add`
  / summon-template / revive-snapshot value always wins). A default can
  even satisfy a required var (e.g. hp). Edited via `!defvar
  add/remove/list` (the var analog of `!defpassive`/`!gclamp`; values
  coerce via `_parse_scalar` like `!ent set_var`, dotted paths nest). The
  intended home for `fog_vision_radius`.
- **Visibility rework — REMAINING pieces** (Pieces 1 & 2 shipped):
  - Piece 3 — fog of war, RANGE-ONLY (next up; design locked with the
    user). Per-entity vision-radius var (`fog_vision_radius`, defaulted
    via `default_entity_vars`); a team sees the UNION of cells within
    each alive member's radius (metric = `fog_range_mode`, default
    `square_radius`). Per-match `Match.fog_enabled` (seeded from a
    `fog_enabled_by_default` rule, toggled by `!match fog on|off`,
    survives refresh like access_overrides). HYBRID: engine auto-applies
    fog (paints `fog_glyph` over unseen cells + ANDs "cell seen?" into
    every `*_visible_to` so entities/tiles/zones/corpses in fog hide
    across map + listings) AND exposes `team_sees_cell` / `team_sees_entity`
    / `can_see` primitives for custom formulas. Omniscient POV / `… full`
    / fog-off bypass. LOS (opaque tiles blocking sight) and explored
    memory are SEPARATE future pieces, explicitly out of scope.
  - Possible later: per-channel auto-routing; richer corpse-snapshot
    introspection (status/vars not exposed today).

Look at recent PR descriptions on the repo (PRs #30 through #35)
for context on the latest design conversations and rationale.

---

## 8. Final advice

**Read this whole file before doing your first edit.** The user has
shipped 35+ PRs of work with prior Claude instances. The codebase has
patterns. Match them. Don't reinvent.

The user is genuinely a great collaborator — clear about goals,
decisive on design questions, appreciative of good work, blunt about
mistakes. Build that trust by being precise and process-disciplined.

When you finish a task, give a **short, factual summary** of what
shipped. Mention the regression count. Flag what you're uncertain
about. Move on to the next thing the user asks for.

Good luck. The system is fun to work on.
