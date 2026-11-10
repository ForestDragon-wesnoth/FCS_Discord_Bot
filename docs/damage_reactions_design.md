# Reactions (#36) and the damage pipeline (#25): design in discussion

Status: the reaction design (§1-§9) is agreed with the user, question by
question, in chat, and BUILT (`reactions.py`, CLAUDE.md §7.23) but for
what §13 lists, which also records the choices made while building. The
damage pipeline (§10) has its first decisions, the ones the reactions
needed; the rest is still to be discussed. §11 lists the details found
open while writing this file; §12 what is set aside for later. Read the
whole file before building on either feature, and ask before deviating
from it.

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
  `incoming.taken > 20`" works), in priority order, up to the per-window
  limit; a reaction without `auto_when` is picked whenever available; a
  unit with nothing qualifying passes. `ask` units ignore `auto_when`.
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

## 10. The damage pipeline (#25): settled so far

- **The pipeline runs in `prepare`**, both sides: the attacker's steps,
  then the defender's passives, resistances and weaknesses. Both numbers
  are kept, as `incoming.damage` (raw, attacker's side) and
  `incoming.taken` (after the defender's side); the names are
  placeholders. Conditions, `auto_when` and the option lines read
  `taken`.
- **The forecast is not trusted** (user call): the body recalculates the
  whole pipeline, attacker passives through the defender's state as it is
  then. `prepare`'s numbers exist for the reactions' convenience.
- **Reactions usually change defender state** (user call): a block
  applies a status, sets a var or writes a key such as `incoming.block`,
  which a defender step reads at its place in the order (FCS1 blocks
  before its capped resistance). Changing the numbers in their own
  `incoming` copy is allowed too; the GM's body decides what it reads.
- **Dice:** attacker dice are rolled in `prepare` and reused by the body
  (`base = roll('2d6')` in `prepare`, `base` in the body). Before the
  body's recalculation the engine rewinds the dice to where `prepare`'s
  pipeline began, so a step that rolls on its own (an armor roll) gives
  the forecast's result when nothing changed.

Still to discuss for #25: how a system declares its ordered step list;
damage packets with several types (Solis Fire Arrow, FCS1 damage and
stamina damage, NCS arcana); conversions between pools (FCS1 Brittle,
Hollow Bones, Freeze; NCS Magical Body); true damage; `deal_damage` and
the `on_damage_taken` / `on_damage_dealt` handlers; how #235 (statuses
that break on damage) and #239 (kill credit) attach.

## 11. Details found open while writing this file

None at the moment.

## 12. Set aside for later (user call)

- **Multi-hit attacks** (several hits on one unit, each with its own
  reaction, NCS "blocking is done individually per incoming hit"): to be
  thought through carefully; `react_point` is only the mid-action hook.
- **Movement-triggered reactions** (opportunity attacks, overwatch): idea
  #263, a later trigger kind under `reacts_on`.

## 13. As built (#36)

Not built yet:

- **Reacting to a reaction** (§8): a reaction's halves open no windows;
  `reactable` and `reaction_depth_limit` don't exist yet.
- **`react_point`** mid-body windows (§3).
- **Discord buttons** for the options (§9, with #42).
- **The dice rewind** before the body (§10): it belongs to the damage
  pipeline (#25), where "where `prepare`'s pipeline began" gets a
  meaning; a body rolling dice of its own would otherwise repeat
  `prepare`'s first rolls.

Choices made while building (open to change):

- Only a typed `!action` / `!ent action` opens windows. An action a
  formula starts (`use_action`) runs at once, and the reply notes that
  reactions weren't offered when some unit holds one that would answer it.
- In a reaction's formulas `target`, `args`, `actor`, `action_name` and
  `action_tags` are the incoming action's; the reaction's own target and
  `k=v` args (typed after its name in `!react`) are `reaction_target` and
  `reaction_args`.
- Auto mode picks only reactions without a target of their own (an auto
  pick has no one to name it).
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
  values are read with `incoming_of(eid)`.
- The match remembers the newest 2,000 reaction uses for
  `reactions_used`; `since='round'` is refused under ATB.
- A picked reaction whose condition is no longer true when the window
  closes, or whose unit is gone, doesn't run; an action whose unit or
  action is gone when the window closes doesn't resolve.
