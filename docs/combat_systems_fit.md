# Combat systems fit: what three tabletop systems need from the bot

Background for feature ideas #200-#255 in `docs/feature_ideas.md`. The audit
fork built three tabletop combat systems that the user and their friends
wrote (Solis Hunting Lodge 2, FCS1 and the New Combat System) inside the bot,
on `836e3af`, to see what the engine can host. §0 summarises each system and
defines the rule terms the ideas use; §1 lists what the bot can't do for
them (L1-L15), §2 what it can do at a needless cost (D1-D18). Each "Fix"
column names the idea that would remove the limit or the cost.

## 0. Background

### 0.1 The three rule sets in brief

- **Solis Hunting Lodge 2 ("Solis").** Players are hunters fighting machines on a square grid (15×11 for hunts, 8×7 for duels).
  - Turn order is fixed at the start by each unit's speed. Tied duelists each roll a d20; in hunts a tied player goes before a machine.
  - On their turn a hunter moves one tile orthogonally (their "standard moves"), then takes one *standard action*, which ends the turn: melee attack, ranged attack, sprint (two more tiles), use a consumable, rest (+10 hp, not within 2 tiles of an enemy), heal an ally, extinguish fire.
  - *Free actions* don't end the turn. *Combo actions* do, but several different ones of the same combo type can be used in one turn.
  - *Reactionary actions* happen during an enemy's turn: dodge (avoid the attack and step one tile) and block (reduce the hit by the weapon's block value). Each has a 3-turn cooldown, and a hunter can use only one reaction until their own next turn.
  - Melee reaches adjacent tiles, diagonals included, and can't hit flying units. Ranged attacks reach a square radius and can't be used while a non-flying enemy stands next to the attacker.
  - After an attack the player may roll a *crit table* (`!roll nothing, nothing, nothing, minicrit, crit`); a crit applies the target's weak-spot effect. A critted hunter loses several actions on their next turn.
  - Armor is *hard* or *soft* and subtracts a flat amount from each hit; hard armor counts half against blunt damage, soft armor half against sharp.
  - Statuses have levels and durations; the special rules are in the glossary (burn, freeze, possession, endurance and so on).
  - Special tiles: fire, freeze, electric and poison tiles that hurt whoever steps on them and then vanish; trees that block movement and catch fire from a burning neighbour; loot boxes; treasure.
  - A unit standing on the map's edge may flee; fleeing hunters earn nothing. The host posts an HP list after every hp-changing action.
- **FCS1.** A grid tactics system with three resources: HP, stamina (spent by most actions, regenerated at the start of each turn) and arcana (for magic).
  - Turn order runs from highest *awareness* down; ties are rolled.
  - Action types: *standard* (ends the turn), *free* (once per turn), *reactionary* (in response to an event, one per event) and *charged*. A charged action puts the unit in a "charging" state that works like a stun for a set number of turns; then the action fires and its target is picked. Some charges are cancelled if the unit takes enough damage meanwhile.
  - Weapons cost stamina per attack and roll crit tables. Statuses carry levels (each level adds damage per turn or a percentage) and apply only when the hit did at least 1 damage.
  - Mounts: a rider and its mount stand on the same tile as two units with separate stats.
  - Perks and enemy abilities add special cases: summons, grabs, area patterns, instakills.
  - Players can walk off the map to flee. Rewards (XP, loot rolls) are computed after the battle.
- **The New Combat System ("NCS").** A JRPG-style system with no map at all: two sides face each other and attacks pick targets by choice.
  - HP, stamina and arcana. HP bonuses multiply (50 base × 1.6 class × 1.3 armor = 104).
  - Turn order by awareness: if one side has the single highest awareness, that whole side acts first, then the other, each side in awareness order.
  - Action types: standard, free, reactionary (during another unit's turn, one per triggering event), *follow-up* (allowed right after a named action), charged (fires at the start of the user's next turn; interrupted by non-light attacks or grapples).
  - The system is built around reactions: every incoming hit can be blocked separately, and several weapons answer an attack before it lands (First Strike, Counter Strike, Stay Back). Grapple attacks can be broken by spending stamina.
  - Damage types fall into physical, magical and other categories. Resistances add up to 50%; past 50% only the single highest one counts.
  - Statuses count down at the end of the affected unit's turn. The design avoids cooldowns and damage over time.

### 0.2 Glossary of rule terms used in this file

| Term | System | Meaning |
|---|---|---|
| Standard / free / reactionary / follow-up / combo / charged action | all | The action types described in §0.1. |
| Standard moves | Solis, FCS1 | Tiles a unit may walk each turn without spending its action. |
| Sprint | Solis, FCS1 | Extra tiles of movement. Solis: costs the standard action. FCS1: a free action paying stamina per tile, and no ranged attacks that turn. |
| Crit table / crit roll | Solis, FCS1 | A list rolled after an attack (`none, none, none, crit`); the result picks the damage line or a special effect. |
| Awareness | FCS1, NCS | The stat that sets turn order. |
| Burn | Solis, FCS1 | Fire damage each turn per level (5 per level). Solis: burning units can't rest, and burn applied to a frozen unit removes the freeze and doesn't apply. FCS1: burn and freeze cancel each other level by level. |
| Freeze | Solis, FCS1 | Solis: damage per turn, no sprint or rest, no dodge from level 3. FCS1: stamina damage per turn, which turns into hp damage past 0 stamina. |
| Bleed | FCS1, NCS | FCS1: damage per turn plus 5 per tile moved. NCS: whenever the unit spends X stamina it takes X true damage. |
| Per-level durations | Solis | Applying a status at a different level keeps each level's own timer ("5 turns of lvl1 burn, 2 turns of lvl2 burn"); only the highest level does anything. |
| Status resistance | Solis, FCS1 | "Lvl2 burn resistance" lowers each applied burn by 2 levels and removes it at 0. FCS1: for statuses without levels it shortens the duration instead. |
| "<status> Immunity" / "<status> Resistance" as statuses | FCS1, NCS | Temporary statuses that protect against another status while they last (FCS1 Antidote: "removes 2 levels of poison and applies lvl1 poison resistance for 2 turns"). |
| Umbral Rot | FCS1 | Damage per turn that also lowers max hp; the lost max hp is shown and can be restored. |
| Plague, Steelmoss | Solis | Damage per turn that grows each turn; the growth resets when the status expires and is kept when it is reapplied. |
| Strength / Vulnerable / Resistance (statuses) | Solis, FCS1 | Percent changes to damage dealt / taken. FCS1: 25% per level, added up, resistance capped at 90%. |
| Efficiency | all | **Different per system.** Solis: every cooldown is 1 turn shorter while it lasts. FCS1 and NCS: the unit may take more than one standard action per turn. |
| Endurance | Solis | Max hp +20% per level, current hp raised by the same percentage; when it ends, current hp stays but is capped at the normal max. |
| Possessed | all | The unit switches to the side of whoever applied the status and is controlled by that person. Solis and FCS1: units it summons while possessed are possessed too. |
| Taunted | NCS | The unit must target the taunter with its attacks; the status ends if the taunter dies or leaves. |
| Sleep (all three), Shutdown (Solis) | | The unit's turn is skipped (FCS1 and NCS: it is forced to use its rest action), and the status ends as soon as the unit takes damage. |
| Stun (all three), Encase (Solis) | | The unit's turn is skipped; Encase also halves the damage it takes. |
| Charging | FCS1, NCS | The state of a unit using a charged action (§0.1). FCS1 shows the damage taken against the cancel threshold ("damage: 25/100"). |
| Rest | all | Solis: +10 hp. FCS1: ×4 stamina regeneration next turn, but double damage from melee until then. NCS: recover stamina, no reactions until the next turn. |
| Take Cover | Solis (enemies) | Immune to ranged attacks until the unit's next turn; not two turns in a row. |
| Bulwark | NCS | Automatically blocks every attack until the user's next turn. |
| Precision | Solis | The next attack crits, then the status ends. |
| Dodge / Block | all | Reactions to an incoming attack (§0.1). |
| First Strike | NCS | Reaction to a melee attack: hit the attacker first; if the attacker blocks or dodges that hit, the original attack is cancelled and its costs are never paid. |
| Preemptive Strike | FCS1 | The FCS1 version: hit a melee attacker first; it doesn't count against the one-reaction limit. |
| Counter Strike | NCS | Reaction: block the attack, then hit back. |
| Stay Back | NCS | Reaction: the *attacker* must choose between cancelling their attack and taking 66 fire damage. |
| Disarming Hook | NCS | A reaction to being hit by First Strike: disarm the striker. A reaction to a reaction. |
| Rushdown | NCS | A shield reaction that automatically blocks counterattacks against the user's first attack each turn. |
| Protect | FCS1 | Reaction when an adjacent ally is attacked: swap places with the ally and take the hit at half damage. |
| Counterspell | FCS1 | Reaction when an enemy within 4 tiles casts a spell: pay 1.5× the spell's cost to cancel it. |
| Emergency Consumable | FCS1 | Reaction: use a consumable just before or after being attacked. |
| Smokescreen | NCS | Reaction: avoid every incoming attack and become invisible for 2 turns. |
| Grapple | NCS | An attack with a "grapple strength"; the defender can spend that much stamina to break it and negate it. |
| Grab | FCS1 (Umbral Hound) | The target is stunned and carried along with the grabber; 400 damage to the grabber in one turn makes it let go. |
| Lethal Determination | FCS1, NCS | If stamina is short for an action, the rest is paid in hp after the action. |
| Masochist, Last Stand, Bloodthirst, Executioner | FCS1 | Perks: gain stamina equal to damage taken; refill stamina and arcana the first time hp drops to 25%; gain extra stamina and hp from kills; an instakill on a target one basic hit would kill. |
| Kill rewards | FCS1 | A base human gains 40% of their stamina capacity per kill; XP and contract rewards count only enemies actually killed. |
| Calculated Strikes, Ceaseless Fortune, Accuracy | FCS1 | Roll the crit table twice and keep a crit; an enemy that crits you must reroll once; remove one "none" from your crit table per level. |
| Brittle, Hollow Bones | FCS1 | Stamina damage taken is converted to hp damage; damage from a hit is increased by that hit's stamina damage. |
| Magical Body, Tireless (Revenant) | NCS | Effects on stamina hit arcana instead; the unit pays action costs in hp. |
| Lance | FCS1 | +10 damage per tile the user moved toward the target this turn. |
| Repeater / Machine Crossbow | FCS1 | Fire several shots in one action, at one target or several. |
| Bat Form | FCS1 | Extra movement that can't be used in the same turn as sprint. |
| Overwhelming Initiative | NCS | Attacks on the user's first turn of each battle deal double damage. |
| Agility Potion | NCS (planned) | "Gain an instant bonus turn." |
| Frost Blast, Stream of Flame, Area Slash, Sorcerous Barrage | FCS1 | Area attacks with fixed shapes aimed in a direction: a 2-long, 3-wide rectangle; a widening flame shape; a set of cells around the user; a 4×3 rectangle whose damage falls off with distance. |
| Fire Arrow | Solis | +25% damage, with the added part dealt as fire damage (one hit, two damage types). |
| Paladin Armor, Tournament Armor | FCS1 | Armor that gives its wearer a status (regeneration, resistance) for as long as it is worn. |
| Umbral Clone | FCS1 | A copy of the user that shares the user's turn and vanishes when damaged. |
| Summoner | FCS1 | An enemy whose charged action summons units costing up to 300 arcana in total. |
| Shrinking Arena, challenge modifiers | FCS1 | Optional difficulty settings. Shrinking Arena removes an edge row or column each turn and kills units on it. Others double enemy hp, or reward "removing 2 'none's from loot rolls". |
| "E"xpand | FCS1 (joke enemy) | Adds rows or columns anywhere in the map, between existing ones too. |
| Executing | FCS1 (joke enemy) | A status during which the unit can't be targeted at all. |
| Value points | Solis | The hunt's reward currency. |
| Size | all | small, medium, large, huge, colossal. Many effects test it ("launch the target if it is medium or smaller"). |

### 0.3 Other references

- **Scripts named in the text** (`solis1.txt`, `ncs.out`, `limits.out`, ...): a script of bot commands and the bot's replies to it from a run on `836e3af`. They live in the audit fork's `audit/combat/`, not in this repo.
- **Document 2** is the audit fork's `FCS_Bot_Audit_Findings_2.md`, the bug-finding audit. Findings named here, all fixed in audit pass 46 (`docs/audit_history.md`):
  - **F21**: `summon(template, x, y, 'deck')` read `'deck'` as the id prefix and made a unit named `deck` on the current map, while `move_entity` and `summon_template` read the same position as the map.
  - **F22**: `!action <eid> <name> <x> <y> map=deck` passed `map=deck` to the action as an argument, so the target cell stayed on the actor's map.
  - **L15**: a set of small coordinate and map messages that didn't mention `map=`.
  - **X4 / X5**: commands measured at 2 s to 60 s (still deferred to the CPU work budget).
- **L1-L15 and D1-D18** are the entries of §1 and §2 below (L15 here is unrelated to document 2's L15).
- **Engine terms**, as CLAUDE.md uses them:
  - a *passive* is a formula attached to a unit, a team or the whole match that runs when a named hook fires (`on_turn_start`, `on_status_added`, `on_var_changed`, `on_entity_step`, …);
  - a status *tick* is the formula a status definition runs at its `tick_when`;
  - a *block condition* is a formula deciding whether a cell blocks a moving unit;
  - `skips_turn` in a status's data skips its bearer's turns;
  - an *action* is data at `vars.<container>.actions.<name>`, run with `!action`; `cmd()` runs a bot command from inside an action, `choose()` asks the player who ran the action a question, answered with `answer=`;
  - `!func` defines a GM formula function;
  - the *access gate* queues a player's state-changing command for a host's approval unless an override allows it.

## 1. What the bot can't do today

No formula or command does these directly: the engine gives them nothing to read or call. "Closest today" is the indirect route a GM can take, and where it falls short.

| # | Can't | Why | Needed by | Closest today | Fix |
|---|---|---|---|---|---|
| L1 | Tell, inside a formula, who typed the command | Formulas get no binding for the person who typed the command. Access overrides work per command (`!host access set action all user=bob`), never per unit. | All three: each player controls their own unit. FCS1 and NCS possession: "the unit is controlled by whoever applied the status". | Approve every player command, or open `!action` to a player, who can then also run the enemies' actions (bob ran the goblin's attack, `access.out`). | #28 + `caller()` (sketched under the idea in `docs/feature_ideas.md`) |
| L2 | Let the defender decide inside the attacker's command | `choose()` asks only the user who invoked the action (through `answer=` tokens or that user's prompt). | Every reaction (§0.2): Solis dodge and block; FCS1 block, dodge, Preemptive Strike, Protect, Counterspell; NCS per-hit block, First Strike, Counter Strike, Stay Back, grapple breaks, Disarming Hook. | Split each attack across commands with a stored pending hit that the defender answers (D2). | #36 slice (sketched under the idea in `docs/feature_ideas.md`) |
| L3 | Ask a specific other player something on Discord | The same: prompts and `answer=` belong to whoever invoked the action. There are no per-player prompts or buttons. | NCS Stay Back, where the attacker must answer the defender's reaction; every reaction played on Discord. | The host relays the question in chat. | #36 (sketched under the idea in `docs/feature_ideas.md`), #42 |
| L4 | Hide the "⚙️ … fired" lines | Every passive that runs prints one (logic.py:5168), and no rule controls it. | All three. A passive given to every unit prints for every unit it runs on, also when its condition does nothing (`ncs.out`: two lines from the bleed passive for units that had no bleed). | None. | #215 |
| L5 | Print text from a status tick, a tile hook or a zone hook | A tick's result is dropped, a tile or zone hook's line has no slot for a result, and `cmd()` exists only in actions. | Every damage-over-time line ("Hopper2 takes 10 burn damage"), fire tiles, auras. | Make the tick write a var that a passive watches, and let that passive return the text, which shows as a "⚙️ … → 'text'" line. | #215 |
| L6 | Show a status's remaining duration to a player | `!ent info` prints names only (`Status: burn`), `!find … show:status` the level (`burn(2)`), and `!list` has only `{status_csv}` (names). `!ent dump` and `!ent status` show everything but wait for host approval (`statusview.out`). | Solis shows each level's timer ("5 lvl1 burn, 2 lvl2 burn"); FCS1 shows end turns ("lvl 1 burn until turn x"); NCS counts timers down. | Passives copy each duration into a var, and `entity_line_format` shows that var. | #222 |
| L7 | Use local variables outside actions | `x = …` in a `!func`, passive or tick body is refused ("Assignment target must be entity[X].path", formula.py:3674). | Damage calculations shared by every attack: armor by damage type, then percentage modifiers, then resistances (all three). | One nested expression per function, or temporaries in `match.*` vars, which then stay in the save, the undo history and the journal. | #216 |
| L8 | Give a status application data that its "status added" hook can read | `status_apply(eid, name, level, duration)` takes nothing else, and `on_status_added` runs before the GM can put data on the new instance (`solis2.out`: "no value at 'by_team'"). | Possession must know whose side to switch to; Taunted must know the taunter; FCS1 weapons cap a status at a weapon-specific level; kill credit for a status's damage needs its source. | A wrapper function does the whole effect itself, and every attack calls the wrapper. | #56 + #227 |
| L9 | Tell, in a block condition, what kind of move it is, or whether the cell is the destination | Block conditions get `tile_x`, `tile_y`, `tile_map` and `self` only. | FCS1 lets units walk over corpses but not stop on one; a moves-per-turn limit should leave knockback alone. | Route movement through an action that checks the destination; for knockback, see the trap in D3. | #220 |
| L10 | Know, in `on_entity_spawned`, who summoned the unit | There is no summoner binding, and `this` is the unit whose turn it is (`limits.out`). | Solis and FCS1: units summoned by a possessed unit are possessed too; summon limits per summoner ("two wolves at a time"). | The summoning action handles it, since it knows `self`. Summons made by passives or ticks can't. | #223 |
| L11 | Give a unit a real extra turn | Formulas can't change the turn order. `cmd('turn set x')` from an action restarts x's turn and re-runs its turn-start passives and ticks: two `turn set`s gave +10 stamina and two burn ticks (`limits.out`). | NCS Agility Potion ("gain an instant bonus turn"). | `turn set`, with a guard in every turn-start passive and tick. | #224 |
| L12 | Count skipped turns in a `schedule_on` delay | A delayed effect fires only on a turn the unit actually starts (logic.py:13625), while status ticks also run on skipped turns. | Charged actions modelled as a stun-like "charging" status (FCS1, NCS). | Count down in the status's own tick (`charge_tick.out`). | #225 |
| L13 | Have units with no position | `!ent add` needs `x y`, and every unit stands on a map. | NCS has no map at all. | A 6×2 strip, one row per side, with `!list` as the board (`ncs.out`). | #226 |
| L14 | Insert rows or columns in the middle of the map | `!map resize` adds and removes rows only at the edges (by anchor). | One FCS1 joke enemy ("E"xpand). | None. | No proposal; only a joke enemy needs it. |
| L15 | Give a unit an id that is a formula binding name (`rider`, `vehicle`, `target`, `slot`, …) | Refused: "names a formula binding or keyword". | FCS1 has a unit called Rider. | Another id; the display name can still be Rider. | None needed. |

## 2. What the bot can do, at a needless cost

Each recipe below was built and run. The cost is what a GM has to write or keep in mind; the trap is what goes wrong when they don't.

- **D1 Action results in the channel.**
  - Recipe: add `eval` to the `action_cmd_allowlist` rule, then end the action with `cmd('eval "\'Hunter1 hits Hopper1 for 30\'"')`. The reply shows ``= `'Hunter1 hits Hopper1 for 30'` `` (`limits.out`).
  - Cost: quotes nested three deep in a line typed into chat, and the line still reads as a formula's value. Every attack in all three systems wants a result line.
  - Fix: #215.
- **D2 Reactions** (an answer from the defender before an attack lands, §0.2).
  - Recipe: the attack writes its hit into `match.pending.<target>.<n>` and ends. Every unit that can react carries one action per possible answer (`take`, `block`, `dodge`, `first_strike`). Each repeats the lookup of the pending hit, the cost and cooldown checks, the charging of the attacker's cost and the cleanup.
  - Size: in `ncs.txt` the three reaction actions (`take`, `block`, `first_strike`) are 28 lines; in `solis1.txt` (`take`, `block`, `dodge`) 25. Most of it is the same lookup and cleanup.
  - The attacker's costs have to be charged when the hit resolves, so that a First Strike can cancel the attack before they are paid.
  - Traps:
    - nothing tells the defender to answer, and nothing lists the open hits;
    - the attacker's turn can move on meanwhile;
    - a reaction to a reaction needs a stack written by hand: NCS Disarming Hook answers a First Strike, and FCS1 Protect moves a hit to a third unit.
  - NCS uses this on almost every attack, FCS1 on most.
  - Fix: a #36 slice (sketched under the idea in `docs/feature_ideas.md`).
- **D3 Turn economy** (one standard action per turn, a number of free moves, once-per-turn free actions, cooldowns).
  - Moves per turn:
    - a block condition `moves_left <= 0`;
    - a passive on each step that counts only the unit's own-turn steps;
    - a passive at turn start that refills `moves_left`.
  - Out of moves, the reply says "Cell (5,5) blocks `hu1`", which reads as a wall.
  - Trap: with the plain condition, a unit that has spent its moves can't be knocked back at all (`push_entity` moved it 0 cells, `pushpit.out`). The condition needs `current_id() == self`, and even then a unit pushed during its own turn after moving stays put.
  - Actions: every action body opens with the same checks and ends by setting them: is it the unit's turn, has it used its standard action (`acted`), has it used this free action this turn, is the action on cooldown, can it pay the cost. FCS1 and NCS Efficiency (several standard actions per turn) turns every flag into a counter.
  - Fix: #24 + #22 + #23 (sketched under the idea in `docs/feature_ideas.md`).
- **D4 Separate durations per level** (Solis: "5 turns of lvl1 burn, 2 turns of lvl2 burn", only the highest level deals damage).
  - Recipe: a `stacks` dict (level → turns left) in the status instance, a six-line `apply_lvl` function, and a nine-line tick using `keys`, `map_with` and `status_del`. It dealt 10, 10, then 5 damage, as the rules say (`solis1.out`).
  - Trap: every attack has to call `apply_lvl`; a plain `status_apply` corrupts the bookkeeping.
  - Fix: #150 with a "highest level acts" mode (sketched under the idea in `docs/feature_ideas.md`).
- **D5 Effects when a status arrives or leaves** (Endurance raising max hp on arrival and capping hp on removal; Possessed switching sides and back).
  - Recipe: one global `on_status_added` passive and one `on_status_removed` passive, each with a branch per status.
  - Cost: every branch's passive fires and prints for every status. The possession branch fired when endurance was applied (`solis2.out`).
  - Fix: #200.
- **D6 Damage calculations shared by all attacks.**
  - Recipe: two to four `!func` expressions, each nested into one line (L7):
    - Solis: armor subtracted (halved against the damage type it is weak to), then the weakness percentage, then the resistance status;
    - FCS1: percentage buffs added up, with resistance capped at 90% on its own;
    - NCS: block first, then resistances added up to 50% (past that the single highest), then weaknesses.
  - All three work (`solis1.out`, `fcs1.out`, `ncs.out`); none is readable.
  - Fix: #216; #25 for ordered steps.
- **D7 Rules about applying statuses.**
  - The rules:
    - burn and freeze cancelling each other (Solis: a burn on a frozen unit removes the freeze and doesn't apply; FCS1: levels subtract);
    - FCS1 weapons that apply a status "stacking up to lvl 2", the cap scaled by the attacker's damage buffs;
    - FCS1 resistance shortening the duration of statuses that have no levels;
    - FCS1 statuses applying only when the hit did at least 1 damage.
  - Recipe: an `apply_*` wrapper function for each rule, and every attack must call the wrapper.
  - The built-ins come close and miss: `blocked_by` refuses the burn but keeps the freeze, `removes` drops the freeze but still applies the burn, `max_level` is one cap per status definition, and resistance lowers levels only.
  - Fix: #227.
- **D8 Charged actions** (§0.1, FCS1 and NCS).
  - Recipe:
    - a `charging` status with `skips_turn`, counting down in its own tick;
    - an hp var hook that cancels it once the damage threshold is reached;
    - a flag that the release action checks (`charge_tick.out`, `fcs1b.out`).
  - Trap: `schedule_on` looks like the right tool and never fires (L12).
  - FCS1 has a dozen charged actions; NCS has charged as an action type.
  - Fix: the `charged` kind in #24 (sketched under the idea in `docs/feature_ideas.md`).
- **D9 Directional area shapes** (FCS1 Frost Blast, Stream of Flame, Area Slash, Sorcerous Barrage: fixed shapes aimed up, down, left or right).
  - Recipe: a four-branch function per shape (Frost Blast in `fcs1b.txt`). Stream of Flame and Area Slash are lists of cell offsets turned four ways by hand.
  - Fix: #219.
- **D10 Placing a tile from an attack** (Solis Flame Throw leaves a fire tile when dodged; FCS1 Scorch Ground sets tiles on fire).
  - Recipe: `tile_set(x, y, '_template', 'fire')`, which writes an internal key; `!tile place` has no formula twin.
  - Fix: #218.
- **D11 List rolls** (`!roll none, none, crit` in all three rule documents).
  - Recipe: a `!table def` for every list before anyone can roll it; `!roll` takes dice only.
  - Fix: #217.
- **D12 Optional action arguments** (`!action hu1 melee ho1 crit=yes`, where `crit` may be left out).
  - Trap: `args.crit` raises "action args has no `crit`" when the player leaves it out (`out_test.out`).
  - Recipe: `'crit' in args and args.crit == 'yes'`.
  - Fix: #228.
- **D13 A rider and its mount sharing a turn** (FCS1: the pair acts in one turn, at the higher awareness of the two).
  - Recipe: give both units the pair's higher awareness and keep the two in sync, so their turns sit next to each other. Their statuses still tick on two separate turns.
  - Fix: #229.
- **D14 Default passives for units already on the board.**
  - Trap: `!defpassive add` reaches only units created afterwards (`fcs1.out`: the stamina regeneration passive never reached the units already placed).
  - Recipe: re-add it per unit with `!foreach`, or use one `!gpassive` that filters.
  - Fix: #221.
- **D15 Setting a system up again for every battle.**
  - Status definitions, global passives, functions, tables, watchers and macros all live in the match.
  - Recipe: keep a prepared match and `!match clone` it for each battle. The test clone carried the definitions, passives, functions and tile templates, and it also copied the prepared match's units and round.
  - Fix: #140, #162.
- **D16 Possession** (§0.2).
  - Recipe: switch the unit's team in a global status hook, with the L8 wrapper carrying the new side. Who may control the unit can't be enforced (L1).
  - Fix: #227, #28.
- **D17 Restrictions on actions.** Melee can't reach flying units; no ranged attack with an enemy adjacent; Disarm forbids weapon actions; Blind forbids ranged; Fear forbids moving toward enemies; a critted Solis hunter can't sprint, rest or use consumables next turn.
  - Recipe: a check at the top of every action the restriction touches. Fear fits in the block condition, by comparing the cell's distance to the nearest hostile with the unit's own (not run).
  - Fix: #234; `requires` on actions in #23 (sketched under the idea in `docs/feature_ideas.md`); #139 for flying as a movement layer.
- **D18 Emoji tile symbols** (Solis draws fire, trees and boxes as 🔥 🌳 📦).
  - The bot accepts them, but in a Discord code block each emoji is two columns wide and shifts the rest of its row. Image mode is fine. No proposal.

## 3. First picks

- **For hosting the three combat systems:** #215 (every system needs it, and it is small); #28 with `caller()` (L1 decides whether a Discord table can play without the host approving every command); the #36 slice and the #24 + #22 + #23 arc (sketched under #36 and #24 in `docs/feature_ideas.md`; together they remove D2, D3 and D8, the bulk of the per-action boilerplate); then #227 and #150.
- **For the bot in general:** #209 (it closes a bug class the engine already guards against in its own fields); #206 and #214 (cheap; one catches bugs in play, the other before a merge); #202 (the formula language is the product, and its reference lives only in the source).
