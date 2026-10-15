# Feature ideas

The open idea list, kept in the repo so it survives between sessions. Numbers
stay fixed so the user can pick by number; new ideas take the next free number
(the highest so far is #199). Items marked (§8) come from CLAUDE.md's deferred
list. When an idea ships, move its number to "Shipped" below; when the user
strikes or rejects one, move it to "Off the list".

**Shipped:** #3, #4, #15, #29, #41, #60, #67, #68, #69, #72, #74, #81, #94,
#99-#103, #106, #109, #110, #113, #125, #126 (as `!dry`), #132, #142, #165-#176,
#179-#181, #183, #186-#188, parts of #14.

**Off the list:**
- Rejected: edge-aware boarding.
- Covered by what shipped: #35 (a preset is a parent system), #85 (`!history
  export` + `!history import` into a new match).
- Struck by the user: #7, #44, #45, #156.

**In progress:** #40 and #164 became the multiple-maps-per-match and nested-maps
work; the agreed design, build order and status are in
`docs/multi_map_design.md` (steps 1 and 2a are on main; next is 2b). Their
entries below keep the original wording for reference.

## Small (a primitive, a rule, or a command)

### Still open

- **#6 Map markers.** `!map mark <x> <y> [glyph] [label] [turns=N]` puts a temporary mark on the map, optionally visible to one team only. Marks show in the legend.
- **#10 Once-per-turn / once-per-round limits on passives.** Today each one needs a counter var plus a second passive that resets it.
- **#11 Summon lifetime.** An `expires_in` template field removes the summon after N turns. It counts on `turns_elapsed`, so it works under ATB.
- **#14 Left-over follow-ups.** Two items remain, both marked "decide later" by you:
  - configurable side names for the directional functions;
  - `to_main_percent` per damage type.
- **#55 Enable/disable passives and watchers.** `!passive disable/enable`, the same on `!gpassive` and `!watch`, plus a formula function. Useful for testing and for "silenced" statuses.
- **#56 Status source tracking.** An optional `source` on `status_apply` records who applied it. That gives kill credit for damage over time, "dispel only your own", and "the curse ends when the caster dies".
- **#57 Repeated and labelled rolls.** `!roll 6x 4d6kh3` rolls six times; `!roll 1d20+5 attack` labels the result.
- **#59 `!turn roll [selector]`.** Rerolls initiative for the selected units with an `initiative_roll_formula` rule.
- **#66 `!redo`.** Keeps the snapshots an undo dropped until the next state-changing command, so an undo can be taken back.
- **#70 Private vars.** A `private_var_prefix` rule hides GM notes and hidden stats from non-hosts, including reads through `$()`.
- **#71 Progress clocks.** `!clock add/tick` keeps named clocks, shown in the `!state` header, firing a `clock_filled` event when one fills.
- **#73 `!rest <selector>`.** Runs a GM-defined `rest_formula` rule on each selected unit.
- **#75 `!remind <n> <text>`.** Posts a message after N turns, counted so it works under ATB.
- **#76 Named map points.** `@gate` works anywhere a command takes `x y`, and `point_x` / `point_y` read it in formulas.
- **#79 `!note add/list/remove`.** Per-match GM notes; pinned notes are visible to players and shown in `!state`.
- **#83 `!roll <dice> <op> <n>`.** A roll against a target, e.g. "17 — success".
- **#87 `line_walkable(eid, x, y)`.** Checks a straight-line walk for the unit's whole body, for charges and dashes.
- **#89 `chance(percent)`** on the match RNG, so it respects `random_seed`.
- **#90 `status_level` / `status_duration`.** Return 0 when the status is missing, so no `status_has` guard is needed.
- **#93 `max_entities_per_match` rule,** a backstop against runaway summon loops in passives.
- **#95 Vision radius through `apply_mods`,** so items and statuses can change sight.
- **#98 Turn-order markers in `!list`.** Marks units that will skip their next turn and units with no initiative.
- **#104 Dice pools in the dice grammar,** the same in `roll()`, `!roll` and `!roll odds`:
  - success counting: `6d10>=8` gives the number of dice at 8 or more;
  - rerolls: `r1`;
  - Fate dice: `4dF`;
  - `d%`.
- **#105 `opposed(spec_a, spec_b)` and `degree(roll, target, step)`.** An opposed roll returns its margin; success tiers cover hard/extreme (CoC) and 7–9/10+ (PbtA). These are generic helpers for the GM's own resolution.
- **#107 Line-of-effect areas.** A `los=True` option on `cells_in_burst` / `cells_in_cone` and `!map preview`, so an explosion stops at walls.
- **#108 Match-wide modifiers.** A `modifiers` bundle in match vars applies to every unit (weather, darkness, an anti-magic field), the match-level twin of team modifiers.
- **#111 `!ent get <id> <path>`.** Reads one var or subtree. Players can use it under the POV rules and #70's private vars; today reading needs `!ent dump` (host-only) or a `$()`.
- **#112 Bar placeholders** for `entity_line_format` and #68: `{hp_bar}` (▰▰▰▱▱, width from a rule), and `{bar:mana:max_mana}` for any pair of vars.
- **#114 Status glyph and color on the ASCII map.** A status definition's `glyph` / `color` (with a priority) show on the bearer. This is the text twin of status overlay sprites.
- **#115 Hidden statuses.** A definition flag (or a visibility formula) keeps a status out of non-host views: `!ent info`, `show:status`, `status:` selectors. For curses and tracking marks.
- **#116 `max_duration` on status definitions,** the duration twin of `max_level`.
- **#117 Zone and tile lifetimes.** `expires_in` on zones and placed tiles, counted on `turns_elapsed` (a 3-turn fire wall, a smoke cloud). It pairs with #11.
- **#118 `zone_grow(name, n)` / `zone_shrink`, plus `!zone grow/shrink`.** Optionally stopped by block conditions; for spreading fire, expanding gas and a closing storm circle.
- **#119 One-shot hooks.** A `once` flag on tile and zone hooks, for pressure plates and mines.
- **#120 `on_move_blocked` hook.** Fires on the mover when a walk, push, pull or swap is stopped, binding the blocker (a unit id or a cell) and the kind of move. It covers bump-to-attack, collision damage on knockback, and "you walk into the wall".
- **#121 Per-unit ATB threshold.** An override var for the `atb_threshold` rule.
- **#122 `transform_keep_vars` rule.** Names vars a transform carries over. The default would be the ATB charge bar, which a transform resets today.
- **#123 `round_end_message_format` and `match_start_message_format` rules,** twins of the round-start line.
- **#124 Initiative reroll every round.** A rule that re-runs #59's formula at each round start.
- **#127 `!eval --trace`.** Lists each var read and write a formula makes, for debugging GM formulas.
- **#128 `!whisper <team> <text>`.** A host posts to every channel bound with that team's POV (handouts, secret info).
- **#129 `!ent export <id>` / `!ent import <json>`.** A unit with its parts as JSON pasted in chat, to share between matches or bots without file access. Import goes through the template checks.
- **#130 Active-unit highlight in ASCII.** Bold or inverted on color surfaces, plus a "now acting" legend line on plain text.
- **#131 `!turn order [n]`.** The next n turns in round mode with skips marked; the round-mode twin of #27.
- **#133 `!map around=<eid> [r]`.** A one-off window centered on a unit; the channel camera stays where it is.
- **#134 `!history note <text>`.** Labels the latest snapshot, so `!history list` and undo previews say what it was.
- **#135 Pending-request expiry rule.** A queued player command expires after N minutes, so a stale request can't be approved hours later.

### New

- **#177 Player command rate limit.** A rule (with an owner ceiling) against command spam once the bot is public.
- **#178 `!owner maintenance on|off [message]`.** Refuses commands on every server while you update the bot, and tells users why.
- **#182 Match threads on Discord.** `!match new ... thread` creates a thread and binds the new match to it.
- **#184 Trail segment spacing above 1** (§8). `segment_spacing` works only in `path` follow mode today.
- **#185 Forced and reflected status transfer** (§8). A transfer that ignores resistance and immunity on the destination, and a "reflect" form that bounces a status back at its source.

## Medium (a subsystem slice)

### Still open

- **#16 Path cost and reachable cells.**
  - `path_cost(eid, x, y)` and `reachable_cells(eid, budget)`;
  - a per-tile `move_cost` field;
  - a diagonal-cost rule.
  - All of these are footprint-aware.
- **#17 `!ent move <id> to <x> <y>`.** Walks the route #16 computes step by step, firing every per-step hook. The GM or player invokes it; the engine only finds the path.
- **#18 Team relations matrix.** Allied / neutral / hostile between pairs of teams, feeding `is_hostile`, the relation filters and disguise checks.
- **#19 Shared vision between allied teams.** Allies see what each other sees under fog. Needs #18.
- **#20 Vision cones.** A `fog_vision_arc` var combined with facing limits what a unit sees.
- **#21 Cover primitive.** `cover_between(a, b)` gives the fraction of the line between two units that is obstructed, footprint-aware.
- **#22 Action cooldowns and charges.** Fields on an action that count uses and turns; the engine refuses the action while on cooldown or out of charges.
- **#23 Declarative action targeting.** `range`, `requires_los` and `relation` fields on an action, checked before the body runs, with the refusal reason reported.
- **#24 Action economy.** `turn_resources` pools (actions, bonus actions, movement) refilled at turn start, plus a `cost` field on actions that spends from them.
- **#25 Damage pipeline with interception.** `deal_damage` runs through GM-ordered steps, with `on_damage_taken` / `on_damage_dealt` handlers that can change the amount (shields, resistances, reflection).
- **#26 Ready / delay a turn.** `!turn delay <after_eid>` moves the current unit to act after another unit this round.
- **#27 ATB turn forecast.** `!turn forecast 8` lists the next eight turns; bare `!turn` shows each unit's charge %.
- **#28 Player-controlled units.** A `controllers` list on a unit; a player's commands on their own unit skip approval.
- **#30 ASCII map import.** A pasted text block plus a glyph → tile template (or unit template) legend builds a board.
- **#31 Prefabs (on hold).** A saved board fragment (units, tiles and zones in a rectangle) stamped into a match at an offset.
- **#32 Procedural generation.** `noise(x, y, seed)` plus room and cave helpers that write tiles.
- **#33 Hidden rolls.** `!roll secret ...` shows the result to hosts only.
- **#34 Session recap export.** The event log as markdown, filtered by POV, for posting a session summary.
- **#136 Status-granting zones (auras).**
  - A zone field `grants_status` puts the status on units inside the zone and removes it when they leave.
  - It follows moves, zone shifts and growth, anchored auras and footprints.
  - "Allies within 2 of the banner get +1 armor" becomes one zone setting; no enter/exit hook pair to write.
- **#137 Zone-of-control events.**
  - A hook fires on a standing unit when a hostile enters or leaves its reach (a `reach` var, footprint-aware, checked per step).
  - The GM's passive decides what happens: opportunity attacks, engagement penalties.
  - The mover's turn keeps going, which keeps this lighter than #36.
- **#138 Tethers.** `tether(a, b, max_dist, mode=drag|block)` keeps two units within a distance, either dragging the follower along or refusing the move. For grapples, leashes, chained prisoners and harpoons.
- **#139 Movement layers.**
  - A per-unit `layer` (ground / air / burrow) with occupancy tracked per layer.
  - A flyer passes over a ground unit or shares its cell, and block conditions can read the layer.
  - Elevation (#37) is separate: that one is terrain height.
- **#140 System-wide status definitions, tables and macros.** `!defstatus`, `!deftable` and `!defmacro` give new matches on a system the GM's definitions, the way `!system tile` / `!system func` / `!system alias` now do for their libraries. Inheritance through the system chain comes with it.
- **#141 User-defined commands.** `!cmd def <name> <usage> <body>` registers a command with named arguments, an access level and help text that runs an action or macro body. Players type `!attack goblin`, and the usage string feeds the stray-word check and Tab completion.
- **#143 Team-based turns.** A `turn_mode` rule (`unit` | `team`). On a team's turn any of its units can act; `!turn next` passes to the next side, and hooks fire per side turn and per unit. Wesnoth / XCOM style.
- **#144 Phase-based rounds.** A `round_phases` list (e.g. move, then act). `!turn next` cycles every unit through phase 1 before phase 2, with phase hooks.
- **#145 Deployment phase.** Before round 1, players place their own units inside their team's deployment zone without host approval. It shares #28's gate change.
- **#146 "What changed" view.** `!map changes` marks the units and cells that changed since the start of the current turn (or since you last looked), for players catching up on Discord.
- **#147 Image-map labels.** Optional HP bars, name labels and a row of status icons on sprites, each toggled by a rule.
- **#148 Live combat log channel.** Event-log lines posted as they happen to a channel bound as `log`, filtered by that channel's POV.
- **#149 Per-unit watchers.** A watcher evaluated for each unit with its own edge state ("the first time any unit drops below 30%", with `self` bound). Today that takes one watcher per unit.
- **#150 Independent status stacks.** A definition mode where each application is its own instance with its own duration (three bleeds on three timers). Today one instance per name carries a level.
- **#151 Named selectors.** `!selector def enemies "team!=red hp>0"`, usable as `!foreach @enemies ; ...` and as `select('enemies')` in formulas.
- **#152 Directional armor and coverage** (§8 combat layers). Per-part, per-side armor values and coverage %, with `armor_at(target, part, side)` and a mitigation helper the GM's damage formula calls. Primitives only.
- **#153 Vehicle follow-ups** (§8). Cover for riders inside a vehicle, a footprint per rider, and shared fuel or initiative between nested vehicles.
- **#154 Spatial index.**
  - A per-cell occupancy index behind `cell_occupant` and the area queries; today they scan every unit, so cell searches cost cells × units.
  - Starts with a measurement on a large map with hundreds of units.
- **#155 GM test runner.** `!test <macro>` runs a macro full of `!assert` / `??` checks in a throwaway clone of the match and reports pass/fail. The scenario harness, for a GM's own rules.
- **#157 Edge-aware `side_hit`** (§8). For long bodies, the body edge an adjacent attacker touches decides the side hit.

### New

- **#189 Game time.**
  - `time_per_turn` / `time_per_round` rules, `game_time()`, and `!time advance 8h`.
  - Schedules and status durations can key on game time, for torches burning out, 10-minute spells and travel between fights.
  - The engine only counts time; what it means stays with the GM.
- **#190 Spectators and table roles.**
  - A spectator binding: a read-only channel with a POV that refuses every state-changing command outright, without queuing it.
  - Named roles (e.g. an assistant GM) carry their own access map.
- **#191 Tiled map import.** A Tiled JSON map becomes tiles through a GID → tile-template legend, and its object layer becomes template spawns. Sprite users can build maps in a real editor.
- **#192 Optional var schemas.** A system can declare types and bounds (`mana: int, min 0`) and refuse writes of the wrong type. Today a string stored where a number belongs fails later and quietly inside a formula.
- **#193 Partial references in statuses and passives** (§8; you said "discuss after partial templates shipped"). Today only var subtrees can reference partials; statuses and passives stay owned by the template.
- **#194 Formula-aware Tab completion.** Inside a quoted formula (`!eval "...`, passive bodies, `!func def`) it completes function names, `entity[` ids and var paths.
- **#195 Revive from a template** (§8). `revive(eid, template=...)` regrows lost parts and restores stats from a template.
- **#196 Deep-illusion disguise** (§8). Today a disguise changes only what outsiders see. This one also fools their targeting and queries (relation filters, `!find`, `nearest_entity`).

## Large (arcs of several PRs)

### Still open

- **#36 Reaction framework.** Interrupts during another unit's turn (block, dodge, counter, opportunity attack), answered through `choose()`. The choice-replay machinery is the groundwork; #137 can trigger it.
- **#37 Elevation.** Per-cell height, high-ground LOS, `height_diff`, and a link to #16's `move_cost`.
- **#38 Light and darkness.** Light sources with radii, per-cell light levels, darkvision; fog and visibility read them.
- **#39 Arbitrary and rotating footprints.** L-shaped and other non-rectangular bodies, and bodies that rotate with facing.
- **#40 Linked maps.** Floors and portals: several matches connected, units moving between them, all with the same owner.
- **#42 Discord interactive layer.** Select menus for `choose()`, action buttons, slash commands, turn pings and timers.
- **#43 History replay.** Stepping through snapshots on a board, or an animated GIF of a session.
- **#158 Campaign roster.** Characters stored at the server level, checked into a match and back out with their state (hp, inventory, XP vars). One roster serves a whole campaign of matches; it builds on the template library.
- **#159 Per-player fog.** Vision per user: each player sees only what their own units see. Today vision is per team and per channel. Needs #28's controllers, plus DMs or per-player channels.
- **#160 Simultaneous turns (WEGO).** Players queue orders, and the GM resolves them all at once in an order a rule decides.
- **#161 Undo tree.** History branches: redoing after a different command keeps both timelines, with `!history branches` to switch. #66 is the small linear version.
- **#162 System packs.**
  - A versioned file bundling a system (with its parent chain), its status definitions, templates, partials, macros and tables.
  - Servers can share a ruleset and upgrade it.
  - Per-server storage (#41) has shipped; the pack still needs #140.
- **#163 Read-only web viewer.** A browser page showing a match's live map from the scene model, with per-team views behind a link token. Needs a web server and real security design.
- **#164 Nested maps.**
  - A small grid owned by a unit (a ship's deck, a building interior) that travels with it; units move between the outer and inner map.
  - #40 links whole matches; this attaches a map to one unit.
  - You said you want this eventually.

### New

- **#197 Scaling.**
  - Covers the spatial index (#154), diff-based undo snapshots (§8: ~3.5 s per command after a whole-map fill on 500×500) and the per-server CPU work budget (§8).
  - These three stand between the bot and public servers, and they share one measurement harness.
- **#198 Play-by-post mode.**
  - Turn deadlines and a ping to the active unit's player (needs #28).
  - A digest of what happened since your last turn (#146).
  - A deadline rule that skips the turn when time runs out; the unit does nothing, so this stays clear of AI.
  - For Discord tables that play over days.
- **#199 Networked GUI client.**
  - The tkinter GUI connects to the running bot and shows a Discord match with that channel's POV, as a richer client for players.
  - Today it only sees the local workspace.
  - Needs an auth token per user and the same security design as #163.

## Suggested groupings

- **Formula ergonomics:** #89, #90, #194
- **Status bundle:** #56, #114, #115, #116, #150
- **Zone bundle:** #117, #118, #119, #136
- **Discord table play:** #128, #148, #182, then #28 and #198
- **Tactical combat:** #16 → #24 → #137 → #36
- **Scaling before a public rollout:** #177, #178, #197
