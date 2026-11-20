# Building a game system on the bot

Notes for whoever writes a full game system on this engine (rules,
functions, statuses, templates, actions), collected from building, playing
and auditing the Solis Hunting Lodge port (the auditor's branch; the
commit hashes in brackets are Solis commits there). Each point came from a
Solis bug or from an engine behaviour met while building it. They are
defaults for what a system's rules text leaves open: the rules text and the
user's answers come first.

When the rules text words something confusingly, skips a case or reads two
ways, ask the user. The Solis ability audit's open questions (when trees
catch fire, the shape of the big ass sword's splash, whether overclock gives
both buffs) each had an answer the text alone didn't settle (5b7e05c).

## Building and installing

- Generate the system from code: one script writes the system file (rules,
  functions, statuses, default passives, tiles, watchers), one the template
  files, and a build step joins them into one `!run` file. Every ability,
  item and enemy is a helper call, so a rule change is one edit and the
  library regenerates. Pass helper arguments past the first two by name:
  `augment(key, name, external=False, m=None)` called with the modifiers
  third made two internal augments external with no modifiers, and no
  template check notices that (fde7cbf).
- Order the file: functions before the rules, statuses and passives that
  call them; a partial before the templates that reference it.
- A function with no parameters: `system func def <system> <name> -
  "<body>"`. A function's value is its last expression line; a body ending
  on an if / else returns the value of the branch that ran (Y5).
- System libraries reach a match when it is created. After changing the
  system, `!match rerun` rebuilds a running match from the system as it is
  now and runs its journal again with the recorded dice (W19); `!match
  sync all` copies the libraries into a live match without re-running
  anything, so a default passive that failed to attach at a spawn stays off
  that unit.
- Defaults go on the system by name (`system defpassive add <system> ...`,
  `system defvar`); per-match switches are default match vars
  (`solis.stealth`, `solis.auto_end_turn`) read with a default, so a match
  made before the var existed plays like a new one (fb3dcd4).
- `!ent set_var u path {}` stores the text "{}": write dicts and lists with
  `var_set` in a formula.

## Data shape

- Abilities are containers `{stats, actions}` over a few shared action
  partials whose body reads `source.stats` (Solis: 15 partials behind every
  enemy ability). The numbers live in the stats and the body is shared, so
  a fix reaches every user of it.
- Generate every description from the stats the effect code reads. A field
  the effect reads and the description skips is a rule the player never
  learns: 18 abilities on 10 enemies lost one this way, the replicator's
  heal on bite among them (176cf5b). A new effect field goes into the
  description generator in the same change.
- Host notes (`template_notes`) hold what the system leaves to the GM.
  Re-read them as the system grows: the quivers' free ammo, the Phoenix
  Amulet, the Avenger's Ring and the injection launcher's bonuses started as
  host notes and each became automatic from existing primitives (4ae56c4,
  5b7e05c). A note that names a command goes stale with it.
- Mark everything a source grants (`class_granted`, `class_item`,
  `from_class`, `avenger_granted`, `summoned_ally`), so taking it off
  removes exactly what it added (a class change, a ring going quiet) and
  counts can skip it (summoned allies don't count toward "half the party
  dead") (4c5bb7a, 5b7e05c).
- A per-battle allowance several items share is a modifier stat summed over
  every source plus a used counter (`free_ammo`, `free_ammo_used`, 4ae56c4).
  A per-status damage bonus is a `<status>_damage` modifier the over-time
  function reads with `apply_mods` (fde7cbf).

## One attack pipeline

- Every attack (generic melee / ranged, class attacks, enemy abilities,
  consumables) goes through one plan function (weapon, reach, crit, damage,
  splash, ammo) and one hit function (damage, statuses, knockback, lines).
  Each Solis bug of this kind came from a second path: the big ass sword's
  splash ran only in the enemy attack partial (fde7cbf), class attacks made
  with a weapon never set its cooldown (2771365), a precision shot spent the
  whole action of a two-shot crossbow (c91843b), a javelin took the arrow
  loaded for the bow (94fdf82).
- Record the use (cooldown, uses this turn, acted) in the action's cost
  step with `cancelled_action_cost pay`: a dodge of every target cancels the
  action before its body runs, and a record kept in the body left a dodged
  charge with no cooldown (078902a). Whether an attack's side effect also
  happens on a miss depends on the rule: Solis's rules end the attacker's
  invisibility on any attack, so its miss branch reveals the attacker too
  (52bd5e0), while most on-hit effects need the hit. Ask the user when the
  text leaves it open.
- A cooldown starts at the first use of a turn, and the turn's later uses
  pass as repeats (a lightning rain fired once or twice had no cooldown,
  fde7cbf). Key cooldowns per item and attack kind (the siege crossbow's
  ranged cooldown blocked its bash, 2060d4e); the countdown passive takes
  `extra_turns=off` so bonus turns don't count.
- Per-turn records (`turn.*`: targets hit, shots fired, moved) carry "not
  the same target next turn", "each hit on a different target", "first
  shot only" and "after moving"; a second use in a turn merges into the
  turn's records (fde7cbf).
- After anything that can kill (a self-destruct, a lethal tick, a reflected
  hit), check `exists(eid)` before reading the unit again: an action body
  that reads a dead unit raises and the whole action rolls back (the
  kamikaze's attack always failed, fde7cbf).
- Check whom each effect lands on: the carrier's consume healed the
  replicator it ate (fde7cbf).
- Every attack reaction's condition excludes non-attacks: a healing arrow at
  an ally opened a dodge window (and would let a guardian protect against a
  heal) until each condition ended with `not solis_heal_shot(actor,
  action_tags)` (2060d4e).
- A reaction whose `before` half fails is spent and the attack lands: its
  window closed when the pick was made. Put what a reaction refuses (a
  dodge toward the attacker, a step the attack's `prepare` forbids) in
  its `target_condition`: `!react` judges it at the pick, with the attack
  bound (`actor`, `incoming`, `reaction_target`), and a refused pick
  leaves the window open for another try. Keep the `before` half for
  what can only fail once the reaction runs.

## Statuses and time

- Decide when each status acts and when it counts down. Solis: damage and
  healing over time act at the start of the bearer's turn (default passives
  on on_turn_start and on_turn_skipped; a stunned unit's skipped turn fires
  on_turn_skipped before its status ticks) and count down at its end
  through status_tick_formula. A status gained during the bearer's own turn
  is marked fresh and skips that turn's countdown, or it deals one tick
  fewer (954503f, faae0e5).
- Effect data sits in the status definition's data (`over_time: {amount,
  machine_amount, tag, growing, heal}`), read by one function; a count-only
  status needs no tick of its own (status_tick_formula counts a defined
  status without a tick down, 078902a).
- A packet's own line prints before everything a death it causes prints.
  A system that words its own damage lines passes them as `message=` on
  `deal_damage` / `heal` (the damage_message_format placeholders); a
  quiet packet followed by a `say()` puts the say after the death line
  (Solis printed each tick's line from a `damage_forecast` first,
  954503f, a forecast per tick that `message=` replaces).
- Say every effect: each status given (level and turns after resistance),
  each block (the amount stopped), each consumable use (what's left), each
  weak spot. Silent effects were the first match's main complaint (19e06ff,
  2431a74, 8aa4243); one helper (solis_give_status) prints the status lines.
- Bookkeeping statuses stay out of team views (`system status visibility
  <system> <status> False`, d9bd426).

## Engine timing a system works around

- on_death passives fire before the corpse exists, so a unit can't revive
  itself there: a revive-on-death item is a system watcher (`system watch
  add`), polled after each command (5b7e05c).
- Watchers are edge-triggered: one reacting to a changing set (the wearers
  whose party is half dead) compares the set with a copy in a match var and
  updates the copy in its effect (5b7e05c).
- Tile time hooks run once per tile: map-wide work (fire spreading to trees)
  guards itself with a match var keyed on `round_number()`, so the first
  tile's hook does it for all. `tiles_with_tag` reads the tile template's
  tags (`system tile tags`) (5b7e05c).
- Units and tiles share the map's glyphs: tiles take symbols (#, +, &), so
  the legend keeps a tree and a unit named Toby apart (d9bd426).

## Testing

- An install check and a sweep (spawn every template, use every action,
  grep for formula errors) prove the system runs. Solis's sweeps were clean
  while about 25 abilities broke their own rules.
- The audit that found them: the rules text and each template's stats side
  by side, and effect checkers running every attack, area attack, heal,
  buff, summon, tile ability, weapon block, consumable, ammo, shield and
  stat item in a fresh match, comparing hp, statuses and cooldowns with the
  stats (fde7cbf).
- Real characters from player loadouts, played turn by turn: a master
  ranger, a medic ranger and a master berserker each found bugs the sweeps
  and demos missed (c91843b, 2060d4e, 2771365). Full matches as GM and
  players in team-bound channels.
- Demo files with transcripts, diffed after every change.
- Solis's units are all 1x1, so its tests cover no footprints: a system
  with multi-tile creatures runs each ability against a multi-tile target
  too (CLAUDE.md §2).
