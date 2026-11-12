# Reactions (#36) and the damage pipeline (#25): design in discussion

Status: the reaction design (§1-§9) is agreed with the user, question by
question, in chat, and BUILT (`reactions.py`, CLAUDE.md §7.23) but for
what §13 lists, which also records the choices made while building. The
damage pipeline (§10) is agreed the same way and not built yet. §11 lists
the details still open; §12 what is set aside for later. Read the whole
file before building on either feature, and ask before deviating from
it.

The two features share one design because they meet at `incoming`: the
data an action hands to the reactions it provokes, which the damage
pipeline also reads.

## 1. Shape of an action

Every action may carry three formula programs:

- **`prepare`** (optional): runs when the action is used, before any
  reaction window opens. It rolls the action's dice and runs the damage
  pipeline as a forecast (§10). Its locals become `incoming` (§4).
- **`body`**: runs after the before window has closed, with `prepare`'s
  locals bound as ordinary locals, holding the values the reactions left.
  It does the action's work (writes hp, applies statuses, summons ...).
  Its own locals join `incoming` for the after reactions (`incoming.dealt`).
- **`cost`** (optional): a full formula program like the other two, run
  by the engine at the cost point (§7). It replaces #24's `{stamina: 8}`
  shorthand (user call): "pay the rest in hp" is written inside it.

An action without `prepare` works as actions do today: its before
reactions see only the engine's facts (§4), and its after reactions see
the body's locals.

Two more action fields serve reactions:

- **`affects`**: an expression (`actor`, `target`, `args` bound) giving
  the ids of the units the action directly affects. For entity and
  entity_list targets it defaults to the target(s); for a targetless
  action, to no one. An area action without it offers only bystander
  reactions. The body reads the result as `affected`.
- **`target_condition`** (optional): an expression with `self` = the
  actor and `target` = one target, checked when the action is used and
  again at resolution (§6). "Must be visible" is `can_see(self, target)`;
  a range check fits here until #23 brings declared ranges.

## 2. Shape of a reaction

A reaction is an ordinary action (same containers, discovery, tags,
restrictions) with reaction fields:

- **`reacts_to`**: a tag filter on the incoming action's tags, in the
  full filter grammar of §7.20 of CLAUDE.md (`melee`, `"spell and (fire
  or frost)"`). It is the coarse filter, checked first. Without it the
  reaction matches every action (user call); a filter that doesn't parse
  matches nothing, and `!action info` names the error.
- **`reacts_on`**: `target` (default) or `any`.
  - `target`: offered when its holder is directly affected (a target, or
    in the area's `affects`), or one of its body parts is while the rule
    `reactions_cover_parts` (default on) is on.
  - `any`: a bystander reaction (counterspell, Protect, a sentry), offered
    on every action passing `reacts_to`, of any target type (a self-heal,
    a targetless action, an area spell). It must have a `condition`, or
    the engine refuses it; a GM who wants one answering every action
    writes `condition: true`.
- **`condition`**: an expression deciding whether the reaction is
  available now (`self` = the reactor; `actor`, `target`, `affected`,
  `incoming` and the engine's facts bound). It does the fine checks:
  distance (there is no range field, user call: distances in games take
  odd shapes), visibility (some reactions work against invisible units,
  some need `can_see(self, actor)`, user call), tag details, limits (§8),
  stamina.
- **`before`** and / or **`after`**: the two halves. Block-then-counter is
  ONE reaction with both halves: picked once, both run.
- **`after_on`**: when the after half runs: `resolved` (default; the
  action went through), `cancelled` (only when it was cancelled), `any`.
- **`priority`**: a number, higher runs first (default 0). Each half may
  carry its own (`before_priority`, `after_priority`), which replaces
  `priority` for that half (user call).
- **`self_trigger`**: default false: a unit's reactions are never offered
  on its own actions. True allows it; a follow-up combo is a reaction to
  its holder's own attack (`self_trigger: true`, an after half), which
  is why #24's `follow_up` field was dropped (user call).
- **`reactable`**: default true; false stops reactions to this reaction.
- **`reaction_on_cancel`**: overrides the rule of that name (§7).
- **`auto_when`**: an expression deciding automatic use (§9).

## 3. The window lifecycle

1. **Use.** An action is used (`!action`, `use_action`, a rider's
   action). The checks that run today run first (restrictions #234,
   target tag filters, `target_condition`). Then `prepare` runs; a
   `prepare` that fails refuses the action before any window opens, as a
   failing body does today (user call).
2. **Who is eligible.** The engine collects reactions from the index of
   units holding reactions (§9): `reacts_on` narrows target reactions to
   affected units, `reacts_to` filters on the action's tags, then each
   remaining reaction's `condition` is read. `self_trigger` and the
   reactor's per-window limit (§8) apply. If nothing is eligible, the
   action resolves at once (step 6).
3. **The window opens** (default mode `ask`, user call: if any reaction is
   available, the game waits). ONE before window covers every affected
   unit and every bystander. The reply lists each unit's options as typed
   commands (§9). The set of units in the window is fixed when it opens.
4. **Picking.** Nothing runs while the window is open. Each unit picks
   (`!react <eid> <reaction> [target] [k=v]`) or passes (`!react <eid>
   pass`), in any order, and can see what others picked. A unit stays
   open until it passes, reaches `reactions_per_window` (rule, default 1,
   -1 = no limit), or has nothing available left; the reply after each
   pick says what it can still add.
5. **Closing.** The window closes when every eligible unit has picked or
   passed, when the host runs `!react skip` (closes every reaction not
   yet triggered; nothing already done is undone, user call), or when
   `reaction_window_expiry_seconds` (rule, default -1 = never) runs out,
   which closes it as skipped.
6. **Before halves run** in one global order (§5), each one's condition
   read again just before it runs. A half that calls `fail()` rolls back
   alone, and the action continues (user call).
7. **Re-read** (§6): who the action affects, and whether each target is
   still valid.
8. **Cancelled?** If a reaction cancelled the action, the body doesn't
   run; costs follow §7. Otherwise **the body runs** once, with
   `affected` and each unit's `incoming`, and the action's `cost` runs.
   A body that calls `fail()` rolls the action back; what happens to the
   reactions that already ran is §7's "a failing body".
9. **After.** After-only reactions get an AFTER WINDOW of their own: a
   separate window, asked again, which a host's skip of the before window
   doesn't close (user call). Once it closes, every after half runs in one
   order (§5): the after halves of two-half reactions picked in the before
   window (each one's `after_on` permitting) together with the after-only
   picks. After windows open only for units the action actually reached,
   plus bystanders; units summoned during the after window are never
   targets of the action.

Several affected units (a list target, an area): one window (step 3),
one `incoming` copy per affected unit (§4), the body runs once, after
windows per unit reached.

Mid-action windows: `react_point('<name>', payload)` in a body opens a
named window at that point, and reactions declare `window: <name>`. The
body re-runs from its start with the reaction's result fed in, as an
answered `choose()` does today. It is for the rare exact-point cases;
multi-hit attacks are set aside (§12).

## 4. `incoming`

`incoming` is a reserved name (like `target` and `actor`): no local,
parameter or unit id can take it.

- It holds **the incoming action's locals**: `prepare`'s before the body,
  plus the body's own after it (user call: reactions read the action's
  variable space, so no action has to decide what to tell them).
- **One copy per affected unit.** `prepare` runs once per action; each
  affected unit gets its own copy of its locals, so B halving its
  `incoming.damage` leaves C's alone (user call). A game rolling per
  target keys a dict by unit id inside `prepare`. The body reads a unit's
  copy with `incoming_of(eid)`; with one target, `incoming` alone. A
  bystander reads the copy of the unit it acts for with `incoming_of`.
- **Control keys**, written by reactions: `cancel` (the whole action),
  `avoid` (only this unit leaves the action), and read-only `cancelled`
  and `cancelled_by` (the unit and the reaction). These four names are
  refused as locals in `prepare` and `body`.
- **The engine's facts stay separate bindings**, which reaction formulas
  already have: `actor`, `target`, `action_name`, `action_tags`, `args`.
  A condition's fine tag checks read `action_tags`.
- **Who may write:** before reactions read and write any key, adding new
  ones too (`incoming.blocked = True`), and the body sees all of it. After
  reactions read only: the body has already run, so a write goes nowhere.

## 5. Order

All picked before halves run in one global order (user call; bunching a
unit's picks makes no sense when one is a cancel and another a counter):

1. effective priority, highest first: the reaction's `priority`, plus the
   rule `reaction_cancel_priority` (default 1000; 0 turns it off) when
   the reaction cancels;
2. ties by the rule `reaction_tiebreak`: `picked` (default, the order the
   reactions were selected) or `turn_order` (the reactors' turn order).

**A reaction cancels** when its before half assigns `incoming.cancel`
anywhere in its text, inside an `if` too (a counterspell that can fail
its roll still counts): detected when the reaction is saved, no field
to declare (user call). `incoming.avoid` (a dodge) earns no bonus (user
call); a GM gives dodges a higher `priority` if they should run early.
`!action info` on a reaction shows "cancels: yes (detected)" and its
effective priority.

**After halves** run in one order once the after window closes (user
call): each half's effective priority (its `after_priority`, else
`priority`), plus the rule `reaction_continuation_priority` for the after
half of a reaction picked in the before window, then the tiebreak. The
continuation bonus (default 500, half the cancel bonus; the two never
meet, since nothing cancels in an after window) lets a block-then-counter's
counter run ahead of after-only picks of the same priority; setting the
rule to 0 turns it off.

## 6. Re-reading the action at resolution

The board can change between declaration and resolution: a dodge moves
its unit, a reaction summons a wall into a fireball's path, a unit turns
invisible. After the before halves have run and before the body runs, the
engine reads again who the action affects (user call; no rule, it always
happens: "if a unit stops being a valid target, it stops being a valid
target"):

- **`affects` is evaluated again.** A unit that left is dropped from
  `affected`, as if it had set `incoming.avoid` (a dodge that moves needs
  no `avoid`). A unit that entered (a summon, a unit pushed in) is added
  with its own copy of `prepare`'s locals; it had no before window and
  gets after windows like any unit reached.
- **Every target is checked again**: the restriction statuses (#234),
  the target tag filters and `target_condition`. A unit that fails drops
  out as if it had avoided. A reaction applying an "invisible" status
  works this way when the action refuses or is immune to such targets.
- **Redirects** (user call): a before half may call `redirect(old,
  new)`, which puts `new` in `old`'s place among the action's targets and
  in `affected`. `new` takes over `old`'s `incoming` copy, and `old`
  leaves the action as if it had avoided it, so FCS1 Protect (swap places,
  take the hit at half damage) is a swap, a redirect and a block applied
  to the copy the protector inherited. Only an action whose target is a
  unit id or a list of ids can be redirected: a targetless action, a
  location target or a unit reached only through an area's `affects` is
  refused, and the refusal fails that half alone (§3 step 6). The re-check
  below judges `new` like any target, so a redirect onto an immune unit
  drops it as invalid.
- **Fewer targets, or none, still resolves** (user call): the body runs
  with what is left (its secondary effects run), the cost is paid, and
  bystanders see the action as resolved. A unit that dropped out sees it
  as cancelled for its own after halves (a dodger's "riposte after a
  miss" fires). This differs from a cancel, which prevents the action.

## 7. Cancels and costs

- **A cancelled action** (`incoming.cancel`) and **a failed one** (its
  body called `fail()` and rolled back) both count as failed for after
  windows: after-only reactions with the default `after_on: resolved`
  aren't offered (user call: a retaliation makes no sense against a spell
  that was fully prevented).
- **Picked reactions on a cancelled action**, before they ran (a fireball
  countered after B and C picked blocks): the rule `reaction_on_cancel`,
  overridable per reaction:
  - `refund` (default): dropped as if never picked; nothing runs, no cost,
    not recorded as used (§8);
  - `keep`: its before half runs with `incoming.cancelled` true; it pays,
    blocks nothing, and counts as used.
- **The cost point** (user call):
  - a reaction's `cost` runs when its half runs (the before half, or the
    after half for an after-only reaction); never under `refund`;
  - the incoming action's `cost` runs when its body runs;
  - a cancelled action's cost follows the rule `cancelled_action_cost`:
    `refund` (default; NCS First Strike: "the attacker never pays") or
    `pay` (the mana for a countered spell is spent), overridable per
    action by a field of the same name.
- A full block that zeroes the damage is no cancel: the action went
  through, so retaliation still fires. A GM who wants a full block to
  stop retaliation writes it as a cancel.
- **A failing body** (`fail()` after reactions ran; user call): by
  default the reactions' effects stay (costs paid, statuses applied,
  recorded as used), and only the body's own changes roll back. `fail`
  takes `refund` among its arguments (`fail(refund)`, `fail('slipped',
  refund)`, `fail('dodge', 'slipped', refund)`; `refund` is a word bound in
  action bodies, like `true`, and can't name a local or a unit;
  `fail('x', refund=True)` works too) to roll the whole exchange back to
  before the window: the reactions' effects and costs too, and nothing
  recorded as used. A failed body pays no `cost`, refund or not.

## 8. Limits and nesting

- **No limit rule** (user call). The engine records every reaction a unit
  uses (the reaction, the action it answered, the turn and round), and
  `reactions_used(eid, since='own_turn', tag=None)` counts them: `since`
  is `own_turn`, `round`, `turn`, `action` (the incoming action now
  resolving) or `ever`. A limit is a line in the condition:
  `reactions_used(self, 'own_turn') < 1` (Solis), `reactions_used(self,
  'action') < 1` (NCS). FCS1's bypass is a reaction whose condition
  doesn't check; a combo simply doesn't count itself. A refunded reaction
  is never recorded.
- **`reactions_per_window`** (rule, default 1, -1 = no limit) is the one
  limit rule, since it decides when a unit is done picking (§3).
- **Reacting to a reaction** is allowed (a reaction is an action), at most
  `reaction_depth_limit` deep (rule, default 2); `reactable: false` on a
  reaction stops it.

## 9. Who answers, and the surfaces

- **Modes** (user call): a per-unit var picks `ask` (the default, needs no
  var), `auto` or `never`; a rule gives the system-wide default (`ask`).
  In `auto` mode the engine picks each available reaction whose
  `auto_when` is true (read against the forecast, so "block only if
  `incoming.damage_taken.hp > 20`" works), in priority order, up to the
  per-window limit; a reaction without `auto_when` is picked whenever
  available; a unit with nothing qualifying passes. `ask` units ignore `auto_when`.
- **Who may react:** `!react` goes through host approval by default, like
  any player command; #28 (player-controlled units) will let a unit's
  players answer directly (user call). A unit's player may pass on its own
  options; the host's `!react skip` closes the window for everyone.
- **The options reply** lists each unit's options as typed commands; each
  line is tagged with its unit, so a channel whose view can't see the unit
  drops the line, and names the actor as the reactor's view sees it (the
  #215 name markers: a unit reacting to an invisible caster reads "react
  to someone's spell"). Discord buttons later, with #42.
- **While a window is open** (rule `reaction_hold_commands`, default on):
  the typed commands `!action`, `!turn next / set / grant`, `!ent move /
  push / pull / swap` and group moves are refused, with a reply saying the
  host must skip the window first (refused, never queued: a queued action
  would run against a board the reactions changed, user call). Every
  other command runs (hosts keep fixing things: `!ent tp`, hp and var
  edits, statuses, tiles, zones). The hold covers typed commands only:
  whatever a formula does inside a command allowed to run goes through
  (moves, summons; an action a formula starts runs without opening
  windows of its own, and the reply says its reactions weren't offered).
- **State, undo and the journal:** the open exchange (the pending action,
  `prepare`'s locals, the windows, the picks so far) is saved with the
  match and survives restarts. The attack, each `!react` and the
  resolution are each their own undo step and journal entry; undoing the
  attack closes the window with nothing having happened.
- **Index:** the engine keeps an index of the units holding reaction
  actions (from their vars, items under the action containers, mount
  slots), kept current by the same change marks as the watcher-holder
  index. A window reads only indexed units, `reacts_to` before any
  condition.

## 10. The damage pipeline (#25)

Agreed with the user question by question, in chat. Built in full
(`damage.py`, CLAUDE.md §7.24); §10.9 lists the choices made while
building. The engine runs a packet of damage through the system's ordered steps and
writes the result; healing runs through its own twin pipeline. The first
four points were settled while designing reactions, the rest after.

### 10.1 With reactions: the forecast and the body

- **The forecast runs in `prepare`.** `damage_forecast(target, damage,
  ...)` (§10.6) runs the same steps as the real hit and then undoes every
  write they made (a block costing stamina, a shield draining), so a step
  needs no side-effect-free twin. Called in `prepare`, it records into
  that target's `incoming` copy, per pool: `incoming.damage_raw` (after
  the attacker's steps) and `incoming.damage_taken` (after the
  defender's), e.g. `incoming.damage_taken.hp`. Conditions, `auto_when`
  and the option lines read them. Healing: `incoming.healing_raw` /
  `incoming.healing_received`.
- **The forecast is not trusted** (user call): the body's `deal_damage`
  runs the whole pipeline again, against the state as it is then.
  `prepare`'s numbers exist for the reactions' convenience.
- **Reactions usually change defender state** (user call): a block
  applies a status, sets a var or writes a key such as `incoming.block`,
  which a defender step reads at its place in the order. Changing the
  numbers in their own `incoming` copy is allowed too; the body decides
  what it reads.
- **Dice:** attacker dice are rolled in `prepare` and reused by the body
  (`base = roll('2d6')` in `prepare`, `base` in the body). Before the
  body's recalculation the engine rewinds the dice to where `prepare`'s
  pipeline began, so a step that rolls on its own (an armor roll) gives
  the forecast's result when nothing changed.

### 10.2 Packets and components

- **A packet** is a list of **components** plus fields for the whole hit
  (user call: "components", so nothing reads as a body part):
  - a component is `{amount, pool, tags}`: `tags` its types (`slash`,
    `fire`, `physical`), `pool` the var it lowers;
  - the packet's own `tags` (`melee`, `ranged`), `attacker` and `source`.
- **Pools.** A pool is any numeric var path (`hp`, `stamina`,
  `resources.arcana`). A component without one hits the rule
  `damage_default_pool` (twin `healing_default_pool`), default `@hp`: the
  var `hp_var` names, the `@<rule>` convention of `transform_keep_vars`.
  Only the hp var kills by default, since the `death_condition` rule reads
  it; a packet lowering only stamina subtracts it and nothing else (user
  call). A system where an empty pool kills says so in `death_condition`.
- **A component's filter tags** are its own tags, the packet's tags and
  its pool as `pool:<path>` (`pool:stamina`). `pool:` is a RESERVED prefix
  (user call): the engine derives it, and no component, packet, unit or
  vocabulary tag may start with it.
- **True damage** is a tag, `true_damage`, with no engine meaning of its
  own: a step's default filter leaves it out (§10.3).

### 10.3 Steps

- **Two ordered lists per system**, `damage_steps` and `healing_steps`, a
  system library like status definitions (user call): copied into each new
  match, the match edits its own copy (`!damage step ...`, `!healing step
  ...`; `!heal` is a common alias name, which a command would forbid),
  the system its own (`!system damage ...`, `!system healing ...`),
  and `!match sync damage` / `sync healing` copies a system's list over a
  live match's.
- **Per-unit effects come in as data the steps read** (user call): the
  systems lean on many effects of single units (a status's resistance, an
  item's damage bonus, a block), which stay modifier records, status data,
  vars or `incoming` keys. Units, statuses and items don't add steps.
- **Fields:** `name` (unique in its list), `side` (`attacker` /
  `defender`), `filter`, `formula` (a program), `description` (optional;
  `!damage explain` and `!damage step list` show it). Placed by list
  position: `!damage step add <name> ... [before=<step> | after=<step>]`,
  `!damage step move`.
- **Two phases** (user call): every attacker step runs, then every
  defender step, each phase in list order. `side` picks the phase and
  `self`. Every system the user plays fits: Solis (Fire Arrow's +25%, then
  armor, weakness, resistance), FCS1 (attacker buffs, then block, the
  defender's percentages, resistance with its 90% cap; the older wording
  "block, then percentage buffs" meant the defender's buffs, user call),
  NCS (block, resistances to 50%, weaknesses). The attacker phase's
  result is `damage_raw`, the defender phase's `damage_taken`.
- **Filter:** a tag filter (`docs/tags_design.md`) on the components'
  filter tags (§10.2). A step without one uses the rule
  `damage_step_default_filter` (twin for healing), default `not
  true_damage`. A step runs once per packet per target, given the
  components its filter matched; with none matched it is skipped.
- **A step whose formula errors** is skipped with a ⚠️ naming the error,
  and the packet goes on (user call).
- **Healing steps** have the sides `giver` / `receiver` and bind
  `healing_giver` / `healing_receiver`, `healing_source`, `healing_tags`,
  `healing_components` (as built: a `healer` binding would forbid that
  unit id).
- **Bindings:** `self` (the step's side's unit), `attacker`, `defender`
  (`attacker` may be None: a lava tile), `damage_source`, `damage_tags`
  (the packet's), `components` (the matched ones, editable: `amount`,
  `pool`, `tags`), `damage_components` (every component, read-only), and
  `incoming` (the defender's copy, when the packet comes from an action
  resolving in a reaction exchange: a block step reads `incoming.block`).
- **Helpers** (user call on the `step_` prefix: `part_` would read as body
  parts, and `damage_part` already exists), acting on the matched
  components, refused outside a step:

  | Helper | Does |
  |---|---|
  | `step_scale(f)` | each amount × f |
  | `step_add(n)` | each amount + n |
  | `step_reduce(n)` | takes n off the matched components as a whole, in order, until n is used up (armor once per hit, however many components) |
  | `step_mods(stat)` | folds each component through `apply_mods(self, stat, amount, its filter tags)`: `inc%` records add up, `more%` multiply, as the records say |
  | `step_move(pool)` | moves the components to another pool |
  | `step_new(amount, pool=None, tags=None)` | adds a component |
  | `step_drop()` | removes the matched components |
  | `step_total()` | the matched components' sum |

- **Examples** (the scenarios use these):
  - Solis armor: `a = entity[self].armor`, halved when hard and a
    component is `blunt` (`for c in components: if 'blunt' in c.tags`),
    then `step_reduce(a)` (filter `pool:hp`).
  - Fire Arrow (attacker, filter `fire_arrow`): `step_new(step_total() *
    0.25, tags=['fire'])`.
  - Strength / Vulnerable: `step_mods('damage_dealt')` (attacker),
    `step_mods('damage_taken')` (defender).
  - FCS1 block, then capped resistance: `if incoming.block:
    step_reduce(incoming.block)`; `step_scale(1 - min(0.9,
    apply_mods(self, 'resist_pct', 0, damage_tags) / 100))`.
  - NCS resistances, up to 50% added, past 50% only the highest.
  - FCS1 Freeze overflow (filter `pool:stamina`): the stamina damage past
    the defender's stamina moves to an hp component. Overflow is the GM's
    step, never an engine feature; one scenario is this recipe, written
    to be copied (user call).

### 10.4 Writing the result

- After the steps, per pool: the components' sum, never below the rule
  `damage_component_floor` (default 0, `none` = no floor; healing twin)
  (user call: damage doesn't heal by default), rounded once by
  `damage_rounding` (floor / round / ceil, default floor; healing twin).
  Amounts stay exact through every step: rounding earlier would lose
  accuracy (user call).
- Each pool is written once through `write_var` (clamps, var hooks,
  partial-template tracking), then ONE death check after all of them.
- **A pool the defender lacks:** the component is dropped, and `!damage
  explain` names it (user call). A step can move it first (NCS Magical
  Body: stamina effects hit arcana).
- **Shields** stay a step the GM places, calling `absorb_damage` (user
  call): the systems put them at different points in the order, and a
  shield step with a `true_damage` filter can take true damage too.
- **Indestructible units** (0/0 parts, `indestructible`) can be hit (user
  call): their steps, line, hooks and records run, nothing is written to
  their own hp, and a 0/0 part still sends its share to its main body.
- **A dead target** is skipped with a note; a corpse can't be a target.

### 10.5 Body parts

- A packet aimed at a body part runs the PART's pipeline (its armor) and
  writes, then sends a share to the main body.
- The part var `to_main_percent` becomes `damage_to_main_percent`, beside
  a new `heal_to_main_percent`, which follows the damage one when unset
  (user call: a stimpack on a limb heals the body a little, as in
  Fallout). The default rule splits the same way:
  `part_damage_to_main_percent_default` and
  `part_heal_to_main_percent_default` (default: follow the damage one).
- Either var may hold a number or a dict of tag filter -> percent
  (`{fire: 100, default: 50}`): each component takes the first entry
  whose filter matches it, else `default` (the per-damage-type share #14
  asked for).
- The share arrives as true damage by default: its components get the
  `true_damage` tag and go through the main body's pipeline, so steps with
  the default filter skip it and a step written for true damage (a shield
  soaking everything) still sees it (user call). One rule per direction
  decides it: `part_damage_share_mode` / `part_heal_share_mode`, `true`
  (default) or `pipeline` (the main body's steps run on it).
- `to_main_cap` caps the damage share only; the heal share stops at the
  main body's max hp through its clamp.
- `damage_spread` and `hit_location` keep choosing the parts; each part's
  share goes through `deal_damage`.

### 10.6 Entry points

- **`deal_damage(target, damage, pool=None, tags=None, packet_tags=None,
  attacker=<default>, source=<default>, quiet=False)`**:
  - `damage` is a number (one component: `deal_damage(t, 20,
    tags=['slash'])`) or a list of components (`[{amount: 20, tags:
    ['slash']}, {amount: 10, pool: 'stamina'}]`); `pool=` fills in each
    component without one, else `damage_default_pool`;
  - `tags` are the components' types, `packet_tags` the hit's;
  - defaults from context: in an action body the actor and the action; in
    a reaction's half the reacting unit and the reaction; in a status
    tick `status_source(self, name)` (#227's `source`, folded into this
    feature, user call) and the status; in a tile or zone hook none and
    the tile / zone; elsewhere none;
  - returns `damage_taken` per pool (`{hp: 18}`);
  - changes the match: banned in `$()`, lines through `surface_log`.
- **`damage_forecast(...)`**: the same arguments, returns `{damage_raw,
  damage_taken}` per pool, changes nothing (§10.1).
- **Healing twins** (user call: healing is something many mechanics care
  about, and it should be customizable too): `heal(...)` returns
  `healing_received`; `healing_forecast(...)`.
- **What counts as damage** (user call): only these. `!ent hp` and a
  direct var write stay silent writes, since a GM has real reasons to set
  hp without anything firing.
- **Commands:** `!ent damage <id> <amount> [pool=<var>] [tags=a,b]
  [packet_tags=...] [from=<unit>] [quiet]` and `!ent heal ...`, gated
  like other `!ent` changes; `!damage explain`, `!damage last` and their
  `!healing` twins (§10.8).
- **`last_damage(eid)`** reads the unit's last packet taken and last
  packet dealt: saved with the match and restored by undo, so a replay or
  a reload reads the same (user call). Healing twin `last_healing`.

### 10.7 After the writes, per target

In order:
1. **Statuses that break on damage** (#235, folded in, user call):
   status fields `removed_on_damage` and `break_at_damage: N`, each with a
   tag filter on what counts, counting only amounts above 0 actually
   taken; heals never touch them.
2. **The damage line** (#247, user call: on by default): rule
   `damage_message_format` (healing twin `healing_message_format`), the
   unit's placeholders plus `{damage_taken.hp}`, `{damage_lost.hp}`,
   `{components}` ("12 slash, 5 fire"), `{attacker}`, `{source}`; tagged
   as a line about the defender, so a view that can't see it drops it.
   `quiet=True` skips it for that packet.
3. **Hooks**: `on_damage_taken` on the defender, then `on_damage_dealt`
   on the attacker (none without one); heals `on_healing_received` /
   `on_healing_given`. Bindings: `attacker`, `defender`, `damage_source`,
   `damage_taken` (per pool, what the steps decided), `damage_lost` (per
   pool, what the var actually moved: 20 taken on 5 hp clamped at 0 is
   lost 5), `damage_components`, `damage_tags`. They fire on a hit that
   came to 0 too (user call). Changing amounts is the steps' job; no hook
   runs before the steps (open, §11).
4. **Recursion:** rule `damage_hook_recursion_limit` (default 16) counts
   nested packets (thorns against thorns); past it packets still apply
   but fire no hooks, with one ⚠️ (user call). The thorns scenario tags
   its returned damage `thorns` and its passive skips packets carrying
   it, the pattern to copy (user call).
5. **Kill credit** (#239's binding, user call): `on_death` binds `killer`
   from the packet that killed. The per-pair damage tally waits for the
   battle report (#240).
6. **The event log** (user call): a packet logs a `damage` / `healing`
   entry (target, attacker, source, per-pool amounts). A raw hp write
   (`!ent hp`, a direct write) logs a renamed `hp_change` entry; a write a
   packet makes logs none. All three are words in `event_log_events`, on
   by default.
7. **The records** `last_damage` reads (§10.6).

An area attack calls `deal_damage` once per target, each running all of
the above.

### 10.8 Reading it

- **`!damage explain <attacker | -> <target> <amount> [pool=] [tags=]
  [packet_tags=]`** runs the forecast and lists each step with its side,
  filter, the components before and after, steps skipped (no match, an
  error) and the writes, rounding included. It names steps and filters,
  never formula text. Host-only (it reads both units' state), like `!ent
  dump`. `!healing explain` twin.
- **`!damage last <unit>`**: the same table for the last packet the unit
  actually took, from the saved record. `!healing last` twin.

### 10.9 As built (choices made while building, open to change)

- **The order after the writes:** statuses that break, the line, the
  event-log entry, the death check (so the line and the entry read before
  the death's), the records, a body part's share and destruction, then
  the hooks. The records come before the hooks, so `last_damage(self)` in
  `on_damage_taken` reads the packet being handled.
- **`on_damage_taken` on a dead defender:** the killing blow's
  `on_damage_taken` fires only while the defender is still in the match
  (a corpse left it); `on_death` (with `killer`) and the attacker's
  `on_damage_dealt` still fire.
- **`damage_lost`** is what the var moved: without a clamp at 0, 40
  damage on 25 hp moves it 40.
- **Break-on-damage fields:** `removed_on_damage` (on / off),
  `break_at_damage: N` (counted in the instance's `damage_counted`,
  removed once it reaches N) and one `damage_filter` (a tag filter) for
  both; a pool's damage taken counts by the share of its positive
  components the filter matches. removed_by `damage`.
- **Status source:** an action body's `status_apply` records the actor
  unless `source=None`; an application without a source leaves an
  earlier one in place; statblocks, templates, prefabs, copies and
  corpses drop it, and the applier leaving the match clears it.
- **Body-part shares:** the base is the components the part took after
  its own steps; the share is worked out before the part's writes (a bad
  knob refuses the hit), sent after them with the packet tag
  `part_share`, and the part's destruction follows the share (as
  damage_part did). The part's hp stops at 0, and a destroyed part still
  passes hits on while its body lives. The heal percent follows: the
  part's `heal_to_main_percent`, else its `damage_to_main_percent`, else
  the heal rule (`same` = the damage rule). A dict percent with no
  `default` key falls back on the rule. The share rounds by
  damage_rounding (half of 5 is 2 by default; rounding_mode now applies
  to transforms only). `damage_part` and `damage_spread` are packets.
- **Forecasts in `prepare`:** several on one target add up per pool; a
  forecast of a unit the action doesn't affect isn't recorded.
  `incoming.damage_taken.hp` reads (a dict from `incoming` reads keys as
  attributes). The dice rewind applies when an exchange opened; after
  the body the RNG never stays behind where it was, so later rolls don't
  repeat the reactions' rolls.

## 11. Details found open while writing this file

- **A hook before the damage steps** (user call: skipped for now, to
  discuss later): a passive that learns a packet is coming before any
  step runs. Reactions already change an action's damage before it
  lands, and the steps change the amounts.

## 12. Set aside for later (user call)

- **Multi-hit attacks** (several hits on one unit, each with its own
  reaction, NCS "blocking is done individually per incoming hit"): on the
  to-do list (user call: reactions per hit, and more customization of how
  multi-hit works). Until then an action that hits twice calls
  `deal_damage` twice, and the reactions answer the action as a whole.
- **Movement-triggered reactions** (opportunity attacks, overwatch): idea
  #263, a later trigger kind under `reacts_on`.

## 13. As built (#36)

Not built yet:

- **A GM-called `react_point()`** (§3): a body opening a window for
  something other than an action it starts (an aura's `on_exit` for an
  opportunity attack, #263). An action a body starts is a react point
  already (below).
- **Discord buttons** for the options (§9, with #42).
- **The dice rewind** before the body (§10): it belongs to the damage
  pipeline (#25), where "where `prepare`'s pipeline began" gets a
  meaning; a body rolling dice of its own would otherwise repeat
  `prepare`'s first rolls.

Actions a formula starts (user calls, after the first build):

- **Inside an action** (a body, a cost, a reaction half, a hook firing
  inside one): `use_action` is an automatic react point. When the action
  it starts needs a window, the whole step (the body, or the half) goes
  back to its start, the window opens on top of the exchange (a stack:
  `Match.reaction_stack`), and when it closes the step runs again from
  its start: the RNG where the step began, every earlier react point of
  the step answered with the picks its window got (`suspended.records`,
  by the order the step reaches them), the action resolving at that
  point with those picks. `use_action` then gives True (resolved) or
  False (cancelled or failed). A window of such an action offers the
  options read when it opened (the re-runs read no conditions for it).
- **Outside any action** (a passive, a watcher effect, a tile hook, a
  status tick, an `!eval`): the action's window opens and the formula
  carries on with `use_action` giving None. While another window is
  open, the action waits in a queue (`Match.reaction_queue`) and its
  window opens when the stack empties, in order.
- **Depth:** the actions a reaction's half starts sit one deeper than
  the exchange it answers (a typed action is depth 0). Past
  `reaction_depth_limit` (default 2) an action opens no windows, nor
  does one a reaction marked `reactable: false` starts; both run at once,
  with a note when some unit holds a reaction that would answer them.
- `fail(refund)` rolls back to the state when the body's resolution
  began in the current command: when the body paused for a react point,
  the reactions that ran in an earlier command stay (the reply says so).
- A failing action takes back the actions it started, a react point's
  reactions included.

Choices made while building (open to change):

- In a reaction's formulas `target`, `args`, `actor`, `action_name` and
  `action_tags` are the incoming action's; the reaction's own target and
  `k=v` args (typed after its name in `!react`) are `reaction_target` and
  `reaction_args`.
- Auto mode gives a reaction taking one unit as its own target the
  incoming action's actor (user call); a reaction taking any other target
  (a location, a list, a corpse) isn't auto-picked.
- A rider is offered its seat's reactions; the vehicle isn't offered
  them.
- A key of `incoming` nobody set reads None, so a body can test
  `incoming.blocked` whether or not a block ran.
- `fail(refund)` rolls back to the state when the window closed: a host's
  edits made while it was open stay.
- `reaction_window_expiry_seconds` counts from when the window opened in
  this bot process; after a restart the count starts again.
- `prepare`'s locals are saved with the open window, so they must be
  values a var can hold; one that isn't (a proxy) is dropped with a
  warning, and the body doesn't see it.
- With several affected units, the incoming action's body starts with the
  locals of the action's own copy (what bystanders write); per-unit
  values are read with `incoming_of(eid)`. A bystander's `incoming` is
  the copy of the one unit affected when exactly one is, else the
  action's own copy (user call).
- The match remembers the newest 2,000 reaction uses for
  `reactions_used`; `since='round'` is refused under ATB.
- A picked reaction whose condition is no longer true when the window
  closes, or whose unit is gone, doesn't run; an action whose unit or
  action is gone when the window closes doesn't resolve.
