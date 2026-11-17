# Feature ideas

The open idea list, kept in the repo so it survives between sessions. Numbers
stay fixed so the user can pick by number; new ideas take the next free number
(the highest so far is #615). #264-#615 come from review passes by
another model; #264-#348 share their numbers with the GitHub issues filed for them. Items marked (§8) come from CLAUDE.md's deferred
list. #200-#255 come from the audit fork's review of three tabletop combat
systems built inside the bot (Solis Hunting Lodge 2, FCS1 and the New Combat
System); their background, the rule terms they use and the limits and costs
they cite (L1-L15, D1-D18) are in `docs/combat_systems_fit.md`, and every
"today" statement in them was checked on `836e3af`. When an idea ships in
full (everything its entry asks for or assumes), its number moves to
"Shipped" below and its entry is removed from the list; an idea shipped in
part keeps its entry, trimmed to what is still open. When the user strikes
or rejects one, it moves to "Off the list".

**Shipped:** #25 (the damage pipeline, with #235, #56 and #227's `source`), #235, #56, #3, #4, #15, #29, #41, #60, #67, #68, #69, #72, #74, #81, #94,
#99-#103, #106, #109, #110, #113, #125, #126 (as `!dry`), #132, #142, #165-#176,
#75, #93, #127, #130, #140, #179-#181, #183, #186-#188, #200-#202, #136, #205, #206, #216-#218, #222, #234, #232, #257, #258, #259, #230 (`!simulate`), #149 (with held, system and formula watchers), #90, #108 (with map-wide bundles), #242 (sizes, with body totals), #261 (the entity description: `!ent info` for players, `short` for the card), #70 (private vars, with team-private ones and private containers), #131 and #27 (the turn forecast, `!turn forecast` and `next_turns`, round mode and ATB), #224 (extra turns, `!turn grant` and `grant_turn`), #122 (`transform_keep_vars`, default the ATB bar), #215 (`say` / `print`, `say_to`, private lines, action `say_format`, `hook_output_lines`), #135 (`pending_request_expiry_seconds`), #221 (`default_passives_existing_units`, on by default), #89, #326, #269, #270, #271, #325 (formula helpers: `chance`, `exists` / `is_alive`, cell sets, `cell_toward` / `cell_behind`, `roll_dice`, dict helpers), #123, #134, #328, #266, #111, #282, #275, #59 and #124 (`!turn roll`, `initiative_reroll_each_round`), #395, #457, #460, #539, #540, #522, #396 (formula helpers: `teams` / `group_names`, `entities_in_cells`, `plural` / `ordinal` / `list_text`, `own_turns`, `fits_at`, `free_cells_near`, `corpse_cell` / `corpses_within`), #587, #590, #605, #610, #600, #434, #538, #453, #456 (destroyed parts marked, `!ent clone` without coordinates, `!zone copy`, table roll counts and `!roll <table>`, `!mount list` for the match, `!ent add` with vars, the empty turn order explained, `!find` aggregates, `!template list has=`), #535, #268, #329, #454 (corpses: read with `!ent info` / `get`, written and looted, dragged, swept with `!find dead` / `!foreach dead`), #104, #361, #253, #459, #362, #520 (dice pools, rerolls, Fate dice and d%; `roll_odds` / `roll_mean`; table roll adjustments, ranks and best / worst; sub-tables and dice in entries; `!table odds`; the action dice line), #154 (spatial indexes: units, zones and corpses by cell), #260 (tags, see
`docs/tags_design.md`), #40 and #164 (multiple and nested maps, see
`docs/multi_map_design.md`), #31 (zone definitions and tags, prefabs, see
`docs/zones_prefabs_design.md`), parts of #14, parts of #190 (Discord-role
capabilities `!server perms` and per-role `!host access ... role=`).

**Off the list:**
- Rejected: edge-aware boarding.
- Covered by what shipped: #35 (a preset is a parent system), #85 (`!history
  export` + `!history import` into a new match), #137 (a reach aura: a zone
  anchored to the unit, whose `on_enter` / `on_exit` hooks fire once per body
  crossing, `zone_anchor_of` naming the standing unit).
- Struck by the user: #57 (labelled rolls), #83 (a roll against a target), #105 (opposed rolls and success tiers compose from `roll()` and `band()`, scenario 1227), #7, #11 (summon lifetime: the summoning action
  gives the summon an expiring status whose on_removed hook despawns it,
  scenario 1089), #44, #45, #156, #145 (deployment phase: a player
  asks the host to place their unit, or the host opens `!ent tp` to them with
  a per-player access override), #163 and #199 (web viewer,
  networked GUI: the CLI / GUI test locally and Discord boards show the live
  map; a separate client matters only if Discord became unavailable).

## Small (a primitive, a rule, or a command)

### Still open

- **#6 Map markers.** `!map mark <x> <y> [glyph] [label] [turns=N]` puts a temporary mark on the map, optionally visible to one team only. Marks show in the legend.
- **#10 Once-per-turn / once-per-round limits on passives.** Today each one needs a counter var plus a second passive that resets it.
  - Note: the limits should key on `turns_elapsed`, so they work under ATB. #24's `uses_per_turn` would cover them.
- **#14 Left-over follow-ups.** One item remains, marked "decide later" by you: configurable side names for the directional functions.
- **#55 Enable/disable passives and watchers.** `!passive disable/enable`, the same on `!gpassive` and `!watch`, plus a formula function. Useful for testing and for "silenced" statuses.
- **#66 `!redo`.** Keeps the snapshots an undo dropped until the next state-changing command, so an undo can be taken back.
  - Note: undo replaces the Match object, and the journal records undo as state. Redo snapshots have to be dropped by any state change on the match from any channel, approvals and watchers included, and a redo needs its own journal entry.
- **#71 Progress clocks.** `!clock add/tick` keeps named clocks, shown in the `!state` header, firing a `clock_filled` event when one fills.
- **#73 `!rest <selector>`.** Runs a GM-defined `rest_formula` rule on each selected unit.
- **#76 Named map points.** `@gate` works anywhere a command takes `x y`, and `point_x` / `point_y` read it in formulas.
- **#79 `!note add/list/remove`.** Per-match GM notes; pinned notes are visible to players and shown in `!state`.
- **#87 `line_walkable(eid, x, y)`.** Checks a straight-line walk for the unit's whole body, for charges and dashes.
- **#95 Vision radius through `apply_mods`,** so items and statuses can change sight.
- **#98 Turn-order markers in `!list`.** Marks units that will skip their next turn and units with no initiative.
- **#107 Line-of-effect areas.** A `los=True` option on `cells_in_burst` / `cells_in_cone` and `!map preview`, so an explosion stops at walls.
- **#112 Bar placeholders** for `entity_line_format` and #68: `{hp_bar}` (▰▰▰▱▱, width from a rule), and `{bar:mana:max_mana}` for any pair of vars.
- **#114 Status glyph and color on the ASCII map.** A status definition's `glyph` / `color` (with a priority) show on the bearer. This is the text twin of status overlay sprites.
- **#115 Hidden statuses.** A definition flag (or a visibility formula) keeps a status out of non-host views: `!ent info`, `show:status`, `status:` selectors. For curses and tracking marks.
  - Note: hidden statuses need the existence-oracle treatment #70's private vars got (`VAR_READER`, CLAUDE.md §7.22): `status:`, `show:status`, `count` and `$()` must treat a hidden status as absent, or a selector answers the question the listing hides. #222's status display has to skip hidden statuses the same way.
- **#116 `max_duration` on status definitions,** the duration twin of `max_level`.
- **#117 Zone and tile lifetimes.** `expires_in` on zones and placed tiles, counted on `turns_elapsed` (a 3-turn fire wall, a smoke cloud).
- **#118 `zone_grow(name, n)` / `zone_shrink`, plus `!zone grow/shrink`.** Optionally stopped by block conditions; for spreading fire, expanding gas and a closing storm circle.
- **#119 One-shot hooks.** A `once` flag on tile and zone hooks, for pressure plates and mines.
- **#120 `on_move_blocked` hook.** Fires on the mover when a walk, push, pull or swap is stopped, binding the blocker (a unit id or a cell) and the kind of move. It covers bump-to-attack, collision damage on knockback, and "you walk into the wall".
- **#121 Per-unit ATB threshold.** An override var for the `atb_threshold` rule.
- **#128 `!whisper <team> <text>`.** A host posts to every channel bound with that team's POV (handouts, secret info).
- **#129 `!ent export <id>` / `!ent import <json>`.** A unit with its parts as JSON pasted in chat, to share between matches or bots without file access. Import goes through the template checks.
- **#133 `!map around=<eid> [r]`.** A one-off window centered on a unit; the channel camera stays where it is.

### New

- **#177 Player command rate limit.** A rule (with an owner ceiling) against command spam once the bot is public.
- **#178 `!owner maintenance on|off [message]`.** Refuses commands on every server while you update the bot, and tells users why.
- **#182 Match threads on Discord.** `!match new ... thread` creates a thread and binds the new match to it.
- **#184 Trail segment spacing above 1** (§8). `segment_spacing` works only in `path` follow mode today.
- **#185 Forced and reflected status transfer** (§8). A transfer that ignores resistance and immunity on the destination, and a "reflect" form that bounces a status back at its source.
- **#203 Where-used search.**
  - `!match search <text>` lists every stored formula and body containing the text, with its location: passives at every layer, unit actions, watchers, status ticks, tile and zone hooks and conditions, functions, aliases, macros, formula rules. `system=<name>` searches a system's rules and libraries.
  - The scanner exists: `_match_formula_slots` (vtt_commands.py:7217) already feeds `!system lint` and the "formulas still mention X" notes of the renames.
  - Host-only, since formulas name hidden units and vars.
  - Answers "which passive writes `stance`?" in a system with dozens of passives.
- **#204 `!var rename <old.path> <new.path>`.**
  - Renames a GM var on every unit, part and corpse of the match, plus `default_entity_vars` and clamp paths, in one undo step. Partial-template markers record the move, and the formulas still naming the old path are listed through #203's scanner, the way `!status rename` lists them.
  - Today statuses, zones, tables, macros and the engine's var-name rules can be renamed (`rename_status`, `_rename_entry`, `rename_unit_vars`). A GM's own var can't: renaming `stance` mid-campaign takes a `!foreach` over every unit plus hand edits of the defaults and clamps.
  - Open choice: templates in the server library get rewritten, or listed.
- **#207 `!hooks <eid> [when]`.**
  - One listing of everything that runs for a unit, per hook name, in the order the engine runs it: global passives, team passives, own passives, attached parts' own passives, status ticks at their `tick_when`, tile and zone hooks under its footprint, its schedules.
  - `!hooks <eid> fire <when> [key=value ...]` (hosts) fires one hook on the unit now. Inside `!dry` that becomes a test that keeps nothing.
  - Today those layers come from seven commands (`!passive list`, `!gpassive list`, `!team passive`, `!status info`, `!tile info`, `!zone hook list`, `!schedule`), and the order is written down only in `fire_hook`.
  - POV: host-only, or filtered to what `!passive list` shows a player today.
- **#212 `!look [eid] [r]`.**
  - A text answer to "what does my unit see": visible units with distance and compass direction (`goblin2: 3 NE, hostile`), tiles and zones with a `legend` label in range, corpses; POV-filtered through the `!find` helpers.
  - For phones, where Discord wraps an ASCII map, and for play-by-post (#198).
  - `!find near:<eid>:<r>` already lists the units, without directions, so this is a presentation shortcut and low priority.
- **#214 CI and kept audit harnesses.**
  - A GitHub Actions workflow running `python run_scenarios.py` and pyflakes on every push and PR. The repo has no `.github/` folder, so the regression runs only when a session runs it.
  - The audit harnesses kept in the repo (`tools/audit/`: stateful chaos, POV leak and existence-oracle detectors, replay differential, renderer parity, suite-as-detector), with a short seeded chaos run in CI. CLAUDE.md §5 has each session rebuild them from descriptions; working copies live in this fork's `audit/doc2/harness/` and drift from main between passes.
  - The combat prototypes in `audit/combat/` can run in CI as well, as a check that real systems keep working.
- **#219 Directional patterns.**
  - `cells_in_pattern(x, y, dir, offsets)`: offsets written for a unit facing `right` are rotated to `up`, `down` and `left`, clipped to the map and charged to `formula_cell_limit` like the other builders. Example: `[[1,-1],[1,0],[1,1],[2,-1],[2,0],[2,1]]` is FCS1 Frost Blast, a rectangle 2 deep and 3 wide in front of the caster. `!map preview pattern` draws it.
  - Open choice: diagonal directions. Rotating a grid shape by 45° isn't exact, so a pattern could carry its own diagonal list, or diagonals could be refused.
  - The game need: FCS1's directional area attacks (docs/combat_systems_fit.md §0.2). Removes D9.
- **#220 Move context in block conditions.**
  - Bindings `final` (true on the last cell of a walk and on the landing cell of a teleport) and `move_kind` (`walk`, `tp`, `push`, `pull`, `swap`) in tile, zone and corpse block conditions.
  - FCS1's "units can walk over corpses but not stop on one" becomes `corpse_block_condition = final`. A moves-per-turn limit becomes `move_kind == 'walk' and moves_left <= 0`, which leaves knockback alone with no `current_id()` trick (the D3 trap).
  - The `block_<kind>` rules stay as the coarse switches.
- **#223 `summoner` binding in `on_entity_spawned`.**
  - The unit (or `None`) whose action, passive or tick summoned the new one. The engine also stores it on the summon (`summoned_by`, a tracked reference under #209), so rules can read it later:
    - "dismiss my summons when I die";
    - "an Alpha Wolf may have two wolves at a time" (NCS);
    - "a Bandit Ringleader can't summon while its last summon lives" (FCS1).
  - Removes L10; possession can then make a possessed unit's summons possessed in a hook (Solis, FCS1).
- **#225 `schedule_on` counting skipped turns.**
  - `schedule_on(eid, delay, body, name, count_skipped=False)`, or a rule. With it on, a stunned or charging unit's delayed effect counts the turns it skips, as status ticks already do.
  - Today the delay waits for turns the unit actually starts (L12), so a charged action built from a `skips_turn` status plus `schedule_on` never fires (`fcs1b.out`: still charging after four rounds).
- **#226 Units without positions (roster mode).**
  - `!ent add <id> <name> <hp>` with no `x y` places the unit on the first free cell of its side's row; `!map mode roster` makes `!map` and the Discord boards show the `!list` roster.
  - For NCS-style fights with no map. The grid stays underneath (the square grid is fundamental), so distance-based functions keep working if a GM wants front and back rows.
- **#228 Declared action arguments with defaults.**
  - An action may carry `args: {crit: 'no', hits: 1}`. Missing ones take the default, unknown ones are refused with the list, `!action info` shows them, and Tab completion offers them; `arg(name, default)` reads one in a body.
  - Today a missing `args.crit` raises (`out_test.out`) and a misspelled key is accepted silently (D12).
- **#229 Shared turn for a rider and its mount.**
  - A slot option `shared_turn`: the rider runs on the vehicle's turn the way an attached body part does (`_attached_tick_parts`), so its statuses tick and its turn passives run on the vehicle's turn, and the vehicle sorts at the pair's higher initiative.
  - The game need: FCS1's mount rules (the rider and mount "share turns" and use "the highest awareness between the units"). Removes D13.
- **#233 Opt-in automatic durations.**
  - A definition field `duration_mode: manual | auto`. With `auto`, the engine lowers `duration` by one at the definition's tick time, after its tick formula runs, and removes the status at 0. `manual` stays the default, as the user decided when status ticks shipped (scenarios 390-391).
  - `expires: next_turn_start`, for effects that last "until your next turn":
    - FCS1 Rest (double damage taken, no blocking);
    - Solis Take Cover (immune to ranged);
    - NCS Bulwark (blocks everything) and NCS Rest (no reactions);
    - Solis's crit penalty (a critted hunter loses several actions on their next turn).
  - Every status in all three systems counts down the same way; each tick in the prototypes repeats the decrement and the removal.
- **#236 Short names for item actions.**
  - `!action hero potion.use` (the item's container name plus the action name) and `!action hero use potion` both reach `inventory.potion.actions.use`.
  - Today two items that both have an action called `use` make `!action hero use` ask for the full path, `inventory.potion.actions.use`, and `inventory.potion.use` is refused (`dupact.out`). Items in all three systems share action names (use, attack, block); the groups' old bot had `!use <item>`.
- **#237 Explicit repeat counts in list targets.**
  - `!action hero light gob*2`: a repeat asked for on purpose, kept apart from an accidental duplicate, which stays merged as the user decided (action.py:915).
  - The game need: attacks that hit the same target several times in one action. NCS light attacks hit twice and "can only hit the same target"; FCS1's Repeater Crossbow "can be used multiple times (including on different targets)", and its Machine Crossbow up to five times.
  - Today the action takes a count argument (`hits=2` in `ncs.txt`) and loops itself.
- **#238 Requirement previews for players.**
  - `!action hu1 melee ho1 check` reports what the action would refuse on (range, cost, cooldown, a forbidding status, not the unit's turn) and changes nothing.
  - It never reports outcomes: `!dry` restores the RNG position, so a full preview would show the roll that will actually happen, and a player could preview until the crit comes up. Outcome previews stay with hosts (`!simulate`).
- **#241 Fleeing as a state.**
  - `flee(eid)` takes a unit out of play into the match's fled list (kept for the report, able to come back), and fires `on_entity_fled`.
  - A rule `off_map_moves: refuse | flee`: with `flee`, a walk off the map's edge makes the unit flee. FCS1: "units can move off the map to flee the match". Solis: a unit "can choose to flee from combat if they are standing next to an edge of the map".
  - Today a walk off the edge is refused ("(1,0) outside 6x3", `names.out`), and the flee action in `solis2.txt` deletes the unit, so nothing remembers that it fled.
- **#243 Per-turn scratch vars.**
  - A var root (rule `turn_scratch_root`, default `this_turn`) emptied at the start of each of the unit's turns, after being copied to `last_turn`; `this_round` is emptied at round start.
  - The game need: rules that remember something until the end of the turn.
    - "Moved this turn", "already used this free action".
    - FCS1 Lance: count the tiles moved toward the target this turn.
    - FCS1 crossbows: "cannot load and sprint on the same turn"; Bat Form: "can't be used on the same turn as sprint".
    - Solis Take Cover: "can't be done two turns in a row", which reads `last_turn`.
  - Today each flag needs its own reset in a turn-start passive (D3). This covers much of D3 before #24 lands.
- **#244 Visible initiative roll-offs.**
  - `turnorder_tiebreaker roll`: ties are broken by a die roll shown in `!turn` ("Hunter1 and Hopper2 tied at 30: 14 against 6").
  - The game need: Solis duels ("they must each do a !roll 20, and whoever rolls higher takes priority"); FCS1 and NCS ("use !roll to determine who goes first"). Today `random_stable` breaks ties silently.
- **#245 Move take-back.**
  - A player may undo their own last move (`!undo move`) while no other command has run since and nothing random happened during the move, so a misclick on Discord costs nothing (as in Battle for Wesnoth).
  - Today undo is a host command over whole commands. Needs #28 to know whose move it was.
- **#246 Step relation helper.**
  - `step_relation(eid, x, y, from_eid)` returns `toward`, `away`, `sidestep` or `none` for a step from the unit's cell to (x, y), relative to another unit.
  - The game need: Solis dodges ("you can't dodge in the direction of the attacking enemy", some attacks "can't be sidestepped" or "can't be dodged backwards") and FCS1 Lance ("only moves towards the target count").
  - It composes today from two distance comparisons; it is a convenience, under the "simplify only what GMs repeat" rule.
- **#247 HP change lines.**
  - Packets print their own lines (shipped with #25: `damage_message_format` / `healing_message_format`); what stays open is a line for a raw hp write:
  - A rule `hp_change_message_format` ("{name} takes {amount} damage ({hp}/{max_hp})", plus a heal twin), printed through the same tagged path as the death lines, so a POV that can't see the unit drops it. Empty (the default) prints nothing.
  - The game need: Solis's rules say "after each hp-affecting action, the host must edit the battlers' hp". Today a write from an action or a tick is silent; only `!ent hp` reports ("Damaged `b` by 3").
- **#248 Roster after each action or turn.**
  - A rule `post_roster: never | action | turn` appends a compact roster to the reply: one `entity_line_format` line per unit, or one line for everyone (`roster_compact_format`).
  - The game need: Solis hosts post "Hawker 1 hp: 100/100, Hunter2 hp: 45/65, …" by hand after every action.
  - The map boards (`!map autoupdate`) already refresh themselves; the roster has no equivalent.
- **#250 A new match from a prepared one.**
  - `!match new <id> <name> <w> <h> from=<prepared>` copies the prepared match's definitions only: no units, round 1, a fresh RNG.
  - Today `!match clone` is the workaround (D15), and it also copies the units and the round (the test clone had "4 entities, round 2").
- **#251 Templates with formulas evaluated at spawn.**
  - A template var may hold `{"__spawn": "50 * level"}`, evaluated once at spawn against the template's other vars and a `level=` given to `!template spawn`.
  - The game need:
    - NCS races are written per level: "Hp: 50 * level, Stamina: 50 * level, Awareness: 50 + 10 per level";
    - FCS1 mounts list a "stat increase per upgrade".
  - One template per race then covers every level. Today it takes a template per level, or an `on_entity_spawned` passive that knows each race.
- **#254 Player shortcuts.**
  - `!me` shows the player's controlled unit, and `!act <action> [target]` runs one of its actions: `!act slash Hopper1` does what `!action hu1 slash ho1` does today.
  - Needs #28 (who controls what) and works best with #255.
- **#255 Targets by display name.**
  - `!action hu1 poke Hopper1` resolves a unique display name when no unit has that id; an ambiguous name lists the ids.
  - Players see names on the map and in `!list`; today only ids work ("Entity 'Hopper1' not found", `names.out`).
- **#256 GUI option: keep the picked map.** A GUI setting (a toolbar checkbox, remembered in `local_settings.json`) so a map picked in the GUI's map menu stays shown until changed, as `!map use` does. Today a pick lasts until the next turn change, like a Discord board's map buttons; the user found that fine for now.

- **#264 Looks by state.** A unit's `looks` list of `{condition, glyph, sprite, color}`; the first entry whose condition holds decides how it draws (bloodied, enraged, burrowed). Today `color` takes a formula and `glyph` / `sprite` are fixed vars.
- **#265 Facing follows movement.** A rule `move_sets_facing` (`off` / `walk` / `all`, with a per-unit override): a step turns the unit the way it stepped. Today a walk leaves facing alone, so a `side_hit` game needs `!ent face` after every move.
- **#267 Statuses that end with their source.** A definition field `source_lost: keep | remove`: when the applier dies or leaves, the status goes (removed_by `source`). For taunts, grapples and concentration buffs; builds on the `__source` field from #25.
- **#272 Default parameters for `!func`.** `!func def mitigated raw,armor=0 "..."`, plus keyword calls.
- **#273 Descriptions on GM definitions.** A `describe` subcommand on `!func`, `!macro`, `!alias`, `!table`, `!watch`, tile templates and zone definitions, shown in their lists and in `!help function`, and ready for the handbook export (#231).
- **#274 Spawn overrides.** `!template spawn orc 3 3 team=red vars.hp=20 name="Orc Chief"` and an `overrides=` dict on `summon_template`. Plain values; #251 covers formulas evaluated at spawn.
- **#277 Explain sight.** `!map los <from> <to> [for=<team>]` walks the line and names the first opaque cell, the condition that made it opaque, the fog range and the visibility condition's result. The sight twin of `!map cell ... for=`.
- **#278 Swallowed-error list.** `!match problems` (hosts) lists the fail-open conditions that errored: block, opaque, visibility, slot and grant conditions, with a count and the last error. Today each shows only where the GM happens to look.
- **#279 Lint for var-path typos.** `!system lint` reports `entity[...].<path>` reads and writes whose path no default var, template or unit holds. A typo in a fail-open block condition currently never blocks and never complains.
- **#280 `!history trace <eid> <path>`.** Walks the autosave diffs and lists each command where the var changed, old → new, with its author. Answers "why is hp 3?".
- **#281 Trace for any command.** `!dry trace <command ...>` gives the `!eval --trace` list (reads, writes, hook writes marked) for a whole action or turn.
- **#283 Send a saves file.** `!store send <name>` posts a file from the saves folder as a Discord attachment (admin or the `files` role). Exports written by `!store save`, `!history export` and `!match journal export` stay on the host's disk, out of a Discord admin's reach. Download only.
- **#284 Access explain.** `!host access check <command ...> [user=|role=]` reports the level that applies and which layer set it: read-only downgrade, elevated arg, per-player, per-role, match or system override.
- **#285 Turn card.** A rule `turn_start_card` (`none` / `card` / `actions`): the turn line is followed by the acting unit's card and its usable action names, as the channel's view sees them.
- **#286 Direction targets.** `target: direction` on an action: `!action hero breath up`, checked against `allow_diagonal_facing`, bound with `.dx` / `.dy`. Pairs with directional patterns (#219).
- **#287 Target count limits.** `min_targets` / `max_targets` on list targets, a number or an expression ("up to 3 targets", "one per level").
- **#288 Tile sprite variants.** `sprite_variants: [grass1, grass2, grass3]` on a tile or template, picked by a hash of the cell so the map stays stable. Graphics only.
- **#289 Host handover.** `!host owner <user>`: the owner passes the match on; an administrator can do it for an owner who left the server.
- **#307 `actions_used(eid, since='own_turn', tag=None, name=None)`.** The action twin of `reactions_used`, over a saved log of successful uses. "One attack per turn" and "no action twice in a row" become one line in a cost or condition, before #24 exists.
- **#308 Own-turn-only actions.** A rule `actions_need_own_turn` (default off) with a per-action `any_time` field and a refusal format rule. Today every body tests `current_id()` itself.
- **#309 Status gain and loss lines.** `status_applied_message_format` / `status_removed_message_format`, a per-definition override, tagged as lines about the unit, empty by default. The status twin of #247; today a status applied by a formula arrives silently.
- **#310 Ticks on the applier's clock.** `tick_when: source_turn_start | source_turn_end`, for "until the end of the caster's next turn". Builds on `__source`, pairs with #267.
- **#311 Zone targets.** Action target types `zone` and `zone_list`: a checked name, seen through the channel's view, bound with `.name` and `.cells`. Doors, chests and traps are zones by the ground / object rule (CLAUDE.md §7.12), and today an action on one takes its name as raw text.
- **#312 Unit-or-cell targets.** `target: entity_or_location`, for a spell cast at a creature or at a point.
- **#313 Relative directions.** `forward`, `back`, `left`, `right` in `!ent move`, `!ent push`, `!ent face` and `move_step`, read from the unit's facing. Pairs with #265.
- **#314 Thick lines.** `width=` on `cells_in_line`, the line searches and `!map preview line`, for beams and charges wider than one cell.
- **#318 Rule categories.** Each registry rule carries a category; `!help rules [category]` browses them. With 600+ rules the only route today is a word search.
- **#321 Per-server command prefix.** bot.py hardcodes `!`, which collides with other bots on a shared server.
- **#322 Storage warning.** Administrators get one line a day once the server passes an owner-set share of its limit. Today the first notice is a rolled-back command.
- **#323 `!help mine`.** The commands and subcommands open to the caller in this channel, judged by the gate with every override. Pairs with #284.
- **#324 `return` in programs.** An early `return <value>` in `!func` bodies, passives and action programs. Today a program's value is its trailing expression, so guard clauses become nested ifs.
- **#327 Tag filter on passives.** `tag=<filter>` on global, team and default passives, tested natively before the formula runs, as `each` watchers already do. It saves a namespace build per unit per hook and removes the guard line from every body.
- **#330 Hit-location odds.** `!part odds <target> <x> <y> [aim=]` prints each part's chance from that position, the way `!roll odds` prints a distribution.
- **#331 Team rename.** `!team rename <old> <new>` moves unit team vars, team data, team passives, colours, POV bindings, reveals, fog memory, `actions_seen` and last actors in one undo step.
- **#333 Concept pages.** `!help topic <name>` for the ideas commands assume: turn models, access and approvals, privacy, tiles and zones, templates / partials / prefabs.
- **#334 Per-channel line detail.** `lines=all|results|none` on `!match bind`, a channel-level `hook_output_lines`, so a player channel shows results while the GM channel shows every ⚙️ line.
- **#335 Zone outlines in images.** A zone look `draw: outline`: only the zone's edge is drawn, so a large aura stays readable over the units inside.
- **#349 Augmented assignment.** `entity[self].hp -= 5` and `total += x`, compiled to a read and a write. Today the sandbox refuses `-=`, so every change spells the path twice.
- **#350 Destructuring.** `x, y = nearest_cell(...)`. Today tuple assignment is refused and a coordinate is read with `coord_x` / `coord_y` (a `for k, v in items(d)` loop already unpacks).
- **#351 Read subscripts.** `cells[0]` and `d['key']`, compiled to `get(...)`; `entity[...]` keeps its meaning.
- **#352 f-strings.** `f"{name} takes {dmg}"`, compiled to `fmt`.
- **#353 Type tests.** `is_number`, `is_text`, `is_list`, `is_dict`, for `!func`s and action args that accept several shapes.
- **#354 Line numbers in formula errors.** A multi-line body's runtime error names the line and quotes it. Today the reply carries the error text alone.
- **#355 Macro variables.** A `let name = $(expr)` line whose value later lines read as `$name`, plus `stop`. Today a macro parks intermediate values in match vars.
- **#356 Actor tag filters.** `actor_requires_tags` / `actor_refuses_tags` on an action, the twins of `target_requires_tags` ("flyers only", "no undead").
- **#357 Hook filters on zones and tiles.** A `filter` (tag filter) and, for an aura, a relation to its anchor, tested natively before the hook formula. The twin of #327.
- **#358 Zone owner.** An owner unit on a zone (`zone_spawn(..., owner=)`), a `zone_owner(name)` read, a rule for the owner's removal (keep / delete), and packets dealt by the zone's hooks defaulting their dealer to the owner. A wall of fire then credits its caster with the kill.
- **#359 Damage breakdown placeholder.** `{breakdown}` in the damage and healing line formats: each step's change ("12 fire − 4 armor = 8"), which `!damage explain` shows hosts today. A rule picks the views that get it.
- **#360 Live numbers in descriptions.** `{...}` placeholders in an action's, item's or status's `description`, filled from the holder's data at read time through the reader, so privacy applies ("Deals {source.damage} fire").
- **#363 Action sweeps queue behind a window.** An `!action` line of a batch or `!foreach` that meets an open reaction window waits in the reaction queue, as formula-started actions already do. Today the hold refuses it, so "every archer fires" stops after the first shot that opens a window.
- **#364 Clamps beyond units.** `!clamp` on team data, match vars and zone data paths: a shared mana pool stops at 0, a door's hp at its maximum.
- **#365 Explain a reaction.** `!react why <eid> [reaction]`: for the open or last window, why the unit was or wasn't offered each reaction (reacts_to, reacts_on, the condition's result or error, its mode, the per-window limit, self_trigger).
- **#366 Explain a grant.** `!zone grant why <zone> <eid>`: the bodies rule, the condition's result, immunity, blocked_by, a part redirect, linger.
- **#367 Explain the turn order.** `!turn why [eid]`: the initiative value, tiebreak var, tiebreaker, team grouping, acted memory and deferred placement behind a unit's slot.
- **#368 Explain privacy.** `!ent privacy <eid> [path]`: the rule, unit list or container marker that makes a path private, and for which readers.
- **#369 What a step triggers.** `!map cell <x> <y> for=<eid>` also lists the tile and zone hooks that would fire for that unit entering or stopping there, by name, under formula_text_visibility.
- **#370 `!template usedby <partial>`.** The templates, prefabs and live units referencing a partial. Today that list appears only inside a rename or delete refusal.
- **#371 Spawn warnings for missing definitions.** A template spawn or summon ends with a ⚠️ naming statuses the match has no definition for and functions its actions and passives call that the match lacks. Prefabs carry their definitions; entity templates arrive without them.
- **#386 Unit-side zone and tile hooks.** `on_zone_entered`, `on_zone_exited` and `on_tile_entered` as passive hooks on the unit, with the zone's name and tags bound. A vampire's sunlight weakness then lives in the vampire; today only the zone or tile can hold the hook.
- **#387 on_kill.** Fires on the killer (own, team and global passives) with `victim` bound. Today "heal when I kill" is a global on_death passive comparing `killer`.
- **#388 on_part_destroyed on the body.** Fires on the root when one of its parts is destroyed, with `part` bound.
- **#389 Blocked status applications.** An `on_status_blocked` hook and a `status_blocked_message_format` line ("Immune!") carrying the reason: immune, resisted to 0, blocked_by, a tag block. Pairs with #309.
- **#390 `move_kind` in movement hooks.** walk / tp / push / pull / swap / carried bound in on_entity_moved, on_entity_step and the tile and zone enter / exit / stop hooks. #220 gives it to block conditions; hooks need it for "the trap springs on a walk, the lava burns on any entry".
- **#391 Actions that end the turn.** An action field `ends_turn` and a formula `end_turn()`: the turn passes once the action and its reaction exchange have resolved, when the actor is the acting unit. Solis's standard action works this way; today the player types `!turn next`, and no formula can advance the turn.
- **#392 Reveal fog from formulas.** `reveal_cells(team, cells, turns=None)` and `reveal_clear`. A flare or a scouting skill needs it; `!reveal_fog` exists as a command only.
- **#393 Parts from formulas.** `part_detach`, `part_attach`, `part_remove`. A lizard dropping its tail, a severed limb becoming a free unit; today the `!part` commands are the only route.
- **#394 Passives from formulas.** `passive_add(eid, id, when, formula, ...)` and `passive_remove`, for an ability learned mid-fight. #55 covers enabling and disabling.
- **#397 Turn-start position.** `turn_start_cell(eid)` and `cells_moved(eid)`, kept by the engine from the unit's turn start. For charge bonuses (FCS1 Lance counts tiles moved), "you may not end where you began" and disengage rules.
- **#398 `!deny <id> <reason>`.** The reason reaches the requester.
- **#399 `!approve <id> always`.** Approving also stores a per-player override for that command and subcommand in the existing `@user` layer, so the same ask skips the queue next time.
- **#400 A `me` token.** `!ent focus <eid>` sets the caller's unit for the channel; `me` then resolves to it wherever a command takes a unit (`!action me slash gob1`, `!ent info me`). A light step toward #254 that needs no controllers.
- **#401 Numbered display names.** A rule that numbers the display names of template spawns sharing a name ("Orc 2"). Ids are numbered already; the names players read on the map and roster stay identical, and #255 depends on unique names.
- **#402 Looks by state for zones and tiles.** #264's looks list on zones, definitions, tiles and templates, with their data in the conditions: a door open or closed, a trap armed or sprung.
- **#403 Facing marker in images.** A rule drawing a small arrow on a sprite that has no per-facing variants. ASCII shows facing through the arrow glyph; such a sprite shows none.
- **#404 Lifetime successors.** With #117: `expires_into: <template|definition>`, so ice becomes water and fire becomes ash.
- **#405 Header line format.** A rule `state_header_format` filling the `!state` / `!list` header from match vars (objective counters, weather, the round's phase).
- **#406 Team card.** `!team info <team>`: members, tags, passives, data, seats and colour in one card with a `team_info_format` rule.
- **#407 Describe a container.** `!ent item <eid> <path>`: what one item gives, namely its tags, actions, modifiers, watchers and resistances, through the reader. Today the pieces sit in `!action list`, `!mod show` and `!watch held`.
- **#408 Explain aliveness.** `!ent why <id>`: the alive and death conditions with their values, a per-unit override, indestructibility, a held death check, the destroyed latch.
- **#409 Stamp remaps.** `remap=wall:ice_wall,orc:orc_elite` on `!prefab stamp` swaps tile templates, zone definitions or unit templates by name for one stamp. One layout serves several biomes or difficulty tiers.
- **#410 Scenario lint.** `run_scenarios.py --lint` lists scenarios with no `!assert` or `??`, prose showing ❌ without the allow marker, and checks placed after `Expected:` where nothing runs.
- **#419 Units that block passage.** A rule `unit_block_condition` (an expression with the mover as `self` and the standing unit as `other`, fail-open, empty by default). Today a walk passes through any unit and only the final cell has to be free: in a probe `a` at (1,1) walked two cells right through `b` at (2,1). "Enemies block, allies let you through" has no switch.
- **#420 Hp follows max hp.** A rule `max_hp_change_mode` (`keep` / `delta` / `ratio`). Today a raised max hp leaves hp where it was and a lowered one clamps it.
- **#421 Event lines for spawns, despawns, mounts and transforms.** Format rules like the death and revive lines, tagged as lines about the unit, empty by default. A summon made by a formula arrives silently today.
- **#422 Grouped damage lines.** A rule `damage_line_grouping` (`per_packet` / `per_action`): one line per action listing its targets ("Fireball: gob1 −8, gob2 −8, gob3 −12"), each target's part shown only to views that see it. An area attack posts a line per target today.
- **#424 Who sees this unit.** `!ent seenby <eid>`: per team, visible or hidden and why (own team, in range of which member, a reveal, line of sight, the visibility condition). Complements #277.
- **#425 `!mod list <eid>`.** Every stat with modifier records on the unit, grouped, with sources. `!mod show` needs the stat's name, so a GM has to know what to ask for.
- **#426 Assertion values.** A failed `!assert` shows both sides of its comparison ("left is 10"). Today the reply is "entity[a].hp == 7 is False".
- **#427 Lint for arity and bindings.** Stored formulas calling a function with the wrong number of arguments or an unknown keyword, or reading a binding their hook never supplies (`killer` in a turn-start passive). The twin of #279.
- **#428 Effective stats in cards.** A placeholder `{mod:<stat>[:<base var>]}` for the roster line, card and description, printing the value through `apply_mods`. Modifiers are pure queries, so the card shows raw vars and players never see the folded number.
- **#429 Preview a pending request.** `!pending <id> preview` runs the queued command under `!dry` as its requester and shows the replies and changes. `!dry` refuses approvals today, so a host approves blind.
- **#430 Sweep summaries.** A `summary` word on `!foreach` and `!batch`: one reply with the outcomes ("18 done, 2 refused" plus the refusals). A sweep posts a reply per line today, up to the message cap.
- **#431 Walrus in expressions.** `(d := entity_distance(self, target)) <= 3 and d > 1`, so a condition computes a value once. Expression-mode formulas can't bind anything today.
- **#432 Results from nested actions.** `action_result(key)` after `use_action` reads the nested body's locals (how much it dealt). Today `use_action` gives True, False or None.
- **#433 Selectors as list targets.** `!action hero bless group:party` and `!action hero volley team=blue near:hero:5`, resolved through the view like `!find`. The scenarios type list targets id by id.
- **#435 Reveal a unit.** `reveal_unit(team, eid, turns=None)` and `!reveal_fog <team> unit <eid>`: the unit shows to that team through fog while the reveal lasts and follows it as it moves. For a tracking dart or a hunter's mark.
- **#436 Containers beyond units.** `var_move`, `item_add` and `item_consume` to and from team data, match vars and zone data. A party stash and a chest that holds its loot; the container functions work on unit vars today.
- **#437 Private zone and tile data.** A `private_zone_data` rule and a per-zone marker, read through #70's reader. In a probe a player's `!zone info chest` printed the chest's whole data, so a chest's loot or a trap's damage is open.
- **#438 Zones listen to events.** `event:<name>` hooks on zones and definitions. A lever emits `lever_pulled` and the door's own hook opens it; today the handler has to be a global passive stored outside the door.
- **#439 Status-carried auras.** A definition field `aura: {definition, radius, metric}`: the engine makes the anchored zone when the status arrives and removes it with the status on every exit path (expiry, death, transform). Composing it takes two hooks today, and each exit path needs its own cleanup.
- **#440 Match trash.** `!match delete` asks for `confirm` and keeps the match with its history and journal in the trash for 24 hours, with `!match restore <id>`. In a probe one typed line deleted a match for good.
- **#441 Sessions.** `!session start [name]` / `end` put marks in the event log and history; `!log session=` and `!history list session=` filter by them, and the recap (#34) can cover one session.
- **#451 Case-insensitive unit ids.** An optional gamerule (off by default): `Hero` and `HERO` resolve to `hero` when one unit matches. In a probe both were refused ("Entity 'Hero' not found"), and phone keyboards capitalise the first letter.
- **#452 More selectors.** `zone:<name>`, `part_of:<eid>`, `riding:<eid>` and `sees:<eid>` for `!find` and `!foreach`. `zone:gas` is an unrecognised predicate today.
- **#455 Cell sweeps.** `!foreach cells:<rect | zone:name | tag:filter> ; <command with $x $y>`. Map building in one line ("fire on every cell of the gas zone"); `!foreach` walks units only.
- **#458 Repeating schedules.** `every=<n>` on `schedule`, `schedule_on` and `!schedule`. Today a body reschedules itself through a `!func`.
- **#461 Passive guards.** `if=<expression>` on any passive, shown in the lists and read natively for the simple shapes the death condition already uses, so a guarded passive skips the namespace build. Pairs with #327.
- **#462 One-shot passives.** A `once` word on a passive: it fires one time and removes itself ("the first time you drop below half"). Watchers have it; passives do not.
- **#463 Passive error limit.** A rule `passive_error_limit`: a passive that errors that many times running is switched off with a ⚠️ until a host re-enables it. A broken global passive prints its error for every unit on every hook.
- **#464 Collapsed repeat lines.** Identical ⚠️ and ⚙️ lines in one reply fold into one with a count ("×12").
- **#465 Per-reaction modes.** `!react mode <eid> <reaction> ask|auto|never`: a player's standing order per reaction ("always dodge, ask about block"). `__reaction_mode` covers all of a unit's reactions at once, and `auto_when` belongs to the GM.
- **#466 Expiring access overrides.** `turns=N`, `until=round_end` and `once` on `!host access`: "bob may `!ent tp` once". Fits the deployment route named when #145 was struck.
- **#467 Team display names.** Team data `name` printed by roster headings and turn lines and offered as `{team_name}`; the team var stays the id.
- **#468 Map on turn start.** A rule `post_map` (never / turn / move): the reply carries the map from the channel's view. Boards edit one pinned message; play-by-post wants a map per turn in the scrollback. The map twin of #248.
- **#469 Tag labels.** A vocabulary entry gains a label (text or emoji) used wherever lines print tags: "🔥 8" in a damage line.
- **#470 Distance units.** Rules for a unit label and scale, so `!dist` and previews print "15 ft"; formulas keep counting cells.
- **#471 Map hooks.** on_enter, on_exit and time hooks stored on a map: the cave applies Darkness to whoever enters. Today that is a global passive on on_entity_changed_map testing `to_map`.
- **#472 Default ground.** A map var naming the tile template every cell without tile data reads (an ocean map, a cave floor). `!tile fill` stores a tile per cell.
- **#473 `!ent diff <id> since=turn|round|<save>`.** What changed on one unit since then. `!ent diff` compares two units, and #280 follows one path.
- **#474 Sight from elsewhere.** `!map ent_sight <eid> at=<x> <y>`: what the unit would see from that cell, for planning a move.
- **#475 Footprint previews.** `!map preview body <eid|template> <x> <y> [facing]`: the cells a body with its located parts and segments would cover there, and whether it fits.
- **#476 Bestiary cards.** `!template card <name>`: a template rendered through the entity description formats under the privacy rules, so players read a monster type the way they read a unit. `!template show` prints JSON.
- **#477 CLI batch mode.** `python cli.py --run <file>` runs a script and exits non-zero on a failed `!assert`. A GM keeping a ruleset's setup and tests in git gets CI for it.
- **#483 Unknown `__` var warnings.** A write of an engine-style `__` key the engine doesn't know (`__reaction_mod` for `__reaction_mode`) ends with a ⚠️ and a close-name suggestion. The per-unit overrides are many, and a typo in one does nothing, silently.
- **#484 Warnings for malformed engine-shaped data.** A write under `actions`, `slots`, `modifiers`, `watchers` or `shields` that the engine couldn't use (an unknown target type, a modifier without a stat) is stored with a ⚠️ naming the problem. Today it surfaces at use, or never.
- **#485 `!why`.** After a ❌, `!why` gives the long form of the caller's last refusal: the check that refused, the rule or condition behind it and where to change it. Refusals stay one line; the detail is on request.
- **#486 Explain a status instance.** `!status view <eid> <name>`: every effective field at the instance's level with its origin (instance, definition, which level entry). Per-level layering makes that hard to read off `!status info`.
- **#487 Explain a clamp.** `!clamp why <eid> <path>`: the clamps that apply (unit, system default), each bound's current value and whether a soft clamp is dormant.
- **#488 Bracket access for statuses and corpses.** `status[self].burn.level -= 1`, `corpse[gob3].inventory`, and `rule.<name>` as a read. Extends #373 to the remaining stores.
- **#489 Passives on several hooks.** `when=on_turn_start,on_revive`: one body, several hooks. Today the passive is added once per hook.
- **#490 Query events.** `ask(name, payload, target=None)` fires the event's handlers and returns their values as a list. A system gets its own extension points ("does anything forbid this?", "bonus to hit from anyone"); `emit` returns nothing to its caller.
- **#491 Turn-clock schedules.** `clock='turns'` on `schedule`: a match-level schedule counting turn boundaries. `schedule` raises under ATB, and `schedule_on` needs a unit.
- **#492 `!func rename`.** Renames a function and rewrites its calls in every stored formula, listing what it changed. Statuses, zones, tables and macros rename this way; `!func` has def and list.
- **#493 Replace across formulas.** `replace=<new>` on #203's search, with a preview: one text swapped in every stored formula and body.
- **#494 Tag-filter blocking.** `block_tags` and `opaque_tags` on tiles, templates and zones (`"not (flying or swimming)"`), read natively against the mover's or viewer's tags. Water that stops non-swimmers needs a condition formula per tile type today.
- **#495 Summed zone grants.** A grant option `combine: highest | sum`: overlapping zones of one definition add their levels ("each fire you stand in adds a level"). Grants take the highest today.
- **#497 Target restrictions from statuses.** Status fields `must_target_source` and `cannot_target_source`, with a tag filter for the actions they cover and an `_unless` expression. Taunt, charm and fear, built on `__source`; the #234 fields restrict by action today, never by target.
- **#498 Confirm list.** A rule naming the commands that need `confirm` (ent remove, map delete, zone del), so a table picks its own safety level.
- **#499 `!again` with a swap.** `!again gob1=gob2` reruns the caller's last command with one word replaced. Ten goblins making the same attack become one typed line and nine short ones.
- **#500 Stepped macros.** `!macro step <name>` runs a macro's next line each call, the pointer saved with the match. A cue list for a scripted encounter: gates open, reinforcements arrive, the bridge falls.
- **#515 What killed it.** The death line names what did the killing: "💀 Goblin died (burn)" or "(Hero's fireball)". Today `death_message_format` offers the unit's own placeholders; the engine knows the killer and the packet's source since the damage pipeline shipped, and on_death binds `killer`, but the line can't print either. Two new placeholders: `{killer}` (a name marker, so each view reads the name it may see) and `{cause}` (the packet's source label: a status, an action, a zone), both empty for a death with no packet behind it; `{?cause?} ({cause}){/?}` keeps the default line clean.
- **#516 Actions that face their target.** An action field `faces_target: true`, and a rule giving the default, on (user call): before the body runs, the actor turns toward its target. Today facing changes only through `!ent face`, `set_facing` or `face_toward`, so a system built on `side_hit` puts `face_toward(self, target)` in every attack body. `run_action` turns the actor through `set_entity_facing` (region parts and seats re-seat) after the checks pass and before `cost`; a list target faces the first one, a cell the cell; an actor that is a glued part turns its body root.
- **#517 Non-stacking modifier groups.** A modifier record may carry `stack_group: "armor_spell"`; of the records sharing a group, only the strongest counts. Today every matching add sums, inc% sums and more% multiplies, so two casts of one shield spell from different sources stack. Before the fold, records sharing a group and an op are reduced to one: the largest for add / inc% / more% / max, the smallest for min; records without a group behave as today. `!mod show` lists the losers as "left out (group armor_spell)". Open question: the winner picked per op (recommended) or across the whole group.
- **#518 Reads of fog memory.** `team_explored(team, x, y)`, `explored_count(team, map=None)` and a host command `!match fog memory clear <team> [map=]` (an undo step). Today `team_sees_cell` answers about sight right now; explored memory is stored per map and drawn by the renderers, and no formula reads it and no command wipes it. The reads go through the map's explored row runs. Uses: "explore 80% of the cave" objectives, a room that springs its trap the first time anyone has seen it, amnesia effects.
- **#519 Manual dice.** `!action hero slash gob1 rolls=17,4` feeds the next `roll()` calls of that command with dice the table rolled by hand. Today `answer=` feeds `choose()` in order; dice always come from the match RNG. Each value replaces one die in the order the body rolls them and is checked against that die's size (a 17 for a d6 is refused); the reply marks them as supplied, and the journal records the command as typed, so a replay reproduces it. A rule `manual_rolls_access` (host default / all) decides who may use it. It also gives scenarios exact dice without seeding.
- **#521 Stamp part of a prefab.** `only=tiles,zones` on `!prefab stamp` and `stamp_prefab`, with the save's four words (units, tiles, zones, corpses). Today `only=` exists on `!prefab save` alone, so a saved fort always arrives with its defenders. An aura whose anchor unit is left out is left out too, with a note, as the save does.
- **#524 Unused definitions.** `!system lint <name> unused` lists what nothing refers to: functions never called, statuses no formula or template applies, tables never rolled, declared tags nothing carries, macros no alias or macro runs. Today lint reports broken formulas and undeclared tags, and dead definitions pile up unnoticed. It reuses the formula-slot scanner behind lint and the rename notes, over the system's libraries, its matches, templates and prefabs. The report is advice; it deletes nothing.
- **#525 What is the match waiting on.** `!match waiting` answers "why is nothing happening?" in one reply. Today `!match info` prints one line, and what holds play up lives in separate commands: an open reaction window and who still owes an answer, pending requests, a pause and its held commands, queued extra turns, schedules and reminders due at the next turn. One section per item present, each through the channel's view (a window's reactors as names the view may see, secret reminders for hosts in a channel without a team view). Players may read it.
- **#526 Player-facing object and terrain descriptions.** `!zone info` and `!map cell` read like `!ent info` does for units: a name, a description and the data the GM chose to show. In a probe a player's `!zone info chest` printed the zone's raw JSON, data and hooks included; units got a player-facing description with #261, objects (zones, by the ground / object rule) did not. Format rules `zone_description_format` and `tile_description_format`, a `description` value on zones, definitions and tile templates, and shown / hidden path lists as the unit description has; hosts keep the raw form with `full`. Builds on #437's privacy.
- **#527 Seat requests.** `!join <team>` lets a player ask for a seat, approved by a host like any queued command (once #294 exists; only a host assigns seats then). The request goes into the approval queue and expires with it; `!leave` gives a seat up. A rule `open_seats` (off by default) lets players take a free seat without approval, for pickup games.
- **#528 Docstring drift guard.** An import-time check that each formula function's documented call form matches its real parameters. Today `!help function` and Tab completion read the call form from the docstring's first line and nothing ties it to the code, so a changed signature leaves the help wrong. It joins the existing drift guards: parse the first docstring line, compare names and count with `inspect.signature`, fail the build on a mismatch, with an allow-list for functions whose docstring shows several forms.
- **#534 A warning for undefined statuses.** Applying a status that has no definition gets noticed. In a probe `!status apply hero brun 2` (a typo of burn) answered "Applied brun to hero", leaving a status with no tick, hooks or modifiers; a formula's `status_apply(self, 'brun')` behaves the same. A rule `status_definition_mode` (off / warn / refuse): under warn the command ends with one ⚠️ naming each undefined status it applied, with a close-name suggestion ("brun: did you mean burn?"); under refuse the application fails. It covers commands, formulas, zone grants and statuses arriving with a spawn. Open question: the default; warn is recommended, since raw statuses without definitions are a supported style and refuse would break it.
- **#537 Hints for common formula mistakes.** The errors a new GM hits first explain themselves. In probes `use_action(hero, 'x', 1)` answered "Unknown identifier 'hero'. Did you mean: 'other'?" and `a && b` "Syntax error: invalid syntax". A small table of recognised mistakes with their own hints: an unknown name that is a unit's id ("`hero` is a unit: write `'hero'` in quotes here, or `entity[hero]` to read its vars"); `&&`, `||`, `!x` ("use and, or, not"); `x++` / `x--` (the supported way to add one); a write to `entity[x].x` or `.y` ("move a unit with move_entity"); a mutating function inside `$()` (name it and point to `!eval`). The generic message stays for everything else.
- **#543 `!done` for seated players.** With team seats (#294), a player ends their own side's turn. Once seats exist, `!turn next` names no unit, so the "commands on your own side's units" rule doesn't reach it and ending a turn waits for a host. `!done` is `!turn next`, allowed when the acting unit belongs to the caller's seat and no reaction window is open; a rule switches it off for tables where the host paces the turns.
- **#544 Pending requests survive a restart.** The approval queue is saved with the match. In a probe a player's request showed in `!pending`, and after `!as restart` the reply was "No pending requests." Requests are saved the way held commands under a pause are, with their queue time, so expiry keeps running on the wall clock. On Discord the old Approve / Deny buttons are gone after a restart; the host notice (#503) or `!pending` lists the requests again.
- **#545 Boards survive a restart.** An auto-updating map board keeps updating after the bot restarts. Today `!map autoupdate` boards are runtime state, and after a restart each is a dead message until someone issues the command again. workspace.json keeps each board's channel, message id, map and options; the adapter re-attaches them lazily at that server's first command, and a board whose message was deleted is dropped quietly.
- **#546 Turn notices reach the acting side.** The turn line also appears in the channel of the team whose unit is up; today it prints where `!turn next` was typed. A rule `turn_notice_channels` (typed default / acting_team): the turn-start line is also posted to channels bound with the acting unit's team view, worded for that view, through the delivery path of #442's reaction prompts. No pings, as the adapter keeps it.
- **#547 A scenario from a journal.** `tools/journal_to_scenario.py <export file>` turns a recorded match into a scenario: the commands as typed, an `!as` switch wherever the author changes, the RNG seed, and `??` checks taken from a replay's replies. A bug met in live play leaves a journal (`!match journal export`), and writing the reproducing scenario is manual work today. The session recorder (#381) covers testing in the CLI; this covers what happened on Discord.
- **#550 Disguises that some teams see through.** A disguise can fool one team and be transparent to another. Today it shows to every viewer outside the unit's own team, so once the party identifies the mimic the GM has to drop it for everyone. The disguise dict gains `seen_through_by`, a list of teams that read the real unit, with `see_through(eid, team)` and `!ent identify <eid> <team>` to add one. Every "is this viewer fooled" question goes through one helper (`_effective_disguise`), which gains the test. Pairs with revealed private vars (#342): an Identify skill does both.
- **#552 Formula conditions in selectors.** `!find where:"entity[self].hp * 2 <= entity[self].max_hp"`, and the same in `!foreach`. Today a selector compares a var with a literal: in a probe `!find "hp<max_hp"` answered "No entities match", silently comparing hp with the text `max_hp`, and comparing two vars of one unit or anything computed has no form. `where:` takes a read-only expression with self as the candidate, run in the caller's reader view (private vars stay private) and charged per unit like a per-unit watcher. Separately, a comparison whose value is the name of a var the units hold ends with a ⚠️ pointing at `where:`.
- **#553 Private passives.** `private=hosts` or `private=team` on any passive. In a probe a player's `!passive list gob` printed the goblin's `secret_trait` with its hook and formula, and `!ent info gob` listed "Passives: secret_trait", so a boss's hidden trait is readable from its first turn. The flag is saved on the passive; lists, the description's passives section and Tab leave a private passive out for readers who may not see it, through the private vars' reader. It still fires as normal, and its ⚙️ line follows `hook_output_lines` as today.
- **#555 Hides follow a parent's rename.** When a parent system renames a library entry, a child's hide of it is renamed with it. Today a child's hidden inherited entry is stored by name, so after `!system status rename` on the parent the hide keeps the old name and the renamed entry shows through (CLAUDE.md §8, "known limitation"). The rename already walks the inheriting systems to refresh their matches; it also rewrites the name in each one's `removed` list for that library, in one undo step with the rename.
- **#556 Spawns that respect blocking.** A rule decides whether a spawn may land on a cell that blocks the unit. Today spawns check occupancy only: in a probe `!ent add gob Goblin 5 3 3 3` put a goblin on a tile with `block` true, and summons, template spawns and prefab stamps do the same (§8, "known issue"). A rule `spawn_block_mode`: ignore (today's behaviour, the default), refuse, and nearest (the closest cell that fits and doesn't block, for the `_near` forms), judging cells as a teleport does (`block_tp`) for the whole footprint and located parts. A stamp under refuse names the blocked units and stamps nothing.
- **#557 Arriving counts as entering.** A rule makes a spawn onto a hooked cell, and a zone created under a unit, fire the enter hooks. In two probes a goblin added onto a tile with an on_enter hook kept its 5 hp, and a fire zone placed under the hero left him at 10 hp until a teleport out and back fired the hook and cost 2. A rule `arrival_fires_enter_hooks` (off by default): with it on, a spawn, summon, revive or stamp fires the tile and zone on_enter and on_stop hooks for the cells it lands on, and a zone appearing over units (created, placed, grown, cells added) fires its enter hooks for them with a binding `zone_created`, as a moving zone binds `zone_moved`. Status grants already settle in these cases; this covers hooks.
- **#558 Context for `!mod show`.** `target=<eid>`, `attacker=<eid>`, `defender=<eid>` and `other=<eid or JSON>` on `!mod show` (and on `!mod list`, #425), passed to the fold exactly as `apply_mods` passes them. The view then shows what a real call computes, and a record that needs context it wasn't given is listed as "needs target" in place of an error.
- **#559 Shaped object definitions.** A zone definition says what shape its objects have: a 2×2 table, a 1×3 barricade. Today `zone_place(definition, x, y)` makes a one-cell zone; a larger object needs `zone_spawn(definition, cells)` with the cells at every use, or `!zone new` plus cell edits. A definition field `shape`, a list of offsets from the placed cell: `!zone place` and `zone_place` build the whole shape, turned by the `facing=` given (zones have a facing since #257), and refuse a shape leaving the map. Without `shape` a placement is one cell as today.
- **#560 A list of valid targets.** `!action hero slash targets` lists who the action can be used on right now; today a player finds out by trying, each wrong try a refusal. It runs the checks before the body (status immunity, the action's target tag filters, `target_condition`) against every unit the channel's view shows and prints those that pass, with the refusal reason for the rest on request. No dice, no costs, nothing changes. Complements the single-target check (#238) and the area preview (#292).
- **#561 Consistent delete verbs.** One verb per meaning across commands. While probing, `!zone del fire` answered "missing arguments": `!zone del` removes a data key and deleting the zone is `!zone drop`, while elsewhere the same act is `!ent remove`, `!alias del`, `!func del`, `!macro remove`, `!status drop`. `remove` deletes the thing everywhere, with `del`, `delete` and `drop` accepted through `_SUB_ALIASES`; deleting a data key becomes `unset` everywhere. Usage strings, help text and scenarios follow; the old meanings go (backwards compatibility is a non-goal, §1).
- **#562 Refusals that point at the nearest form.** A usage refusal shows the one form the caller was closest to. Today `!zone def crate glyph C` was refused with the whole usage of `!zone def`, a dozen alternatives in one line. The stray-word check already tokenises every alternative; it scores them by the literal words typed (`glyph` here), shows the best one or two, and when reordering the typed words satisfies one, suggests it: "did you mean `!zone def glyph crate C`?". The full usage stays one `!help` away.
- **#583 Refusals carry a marker.** A refusal is a kind of reply the engine knows about, whatever its text. Today `_ErrorWatchCtx` decides that a line failed by checking whether the reply starts with ❌ or ❓ Unknown command, so a handler that forgets the emoji silently defeats strict batches, `!simulate` failure counts and the harness (four such replies were found and fixed, scenario 1168). Handlers refuse through one call (`ctx.refuse(text)`, or raising VTTError, which many already do); it adds the ❌ and sets a flag on the context. Strict batches, `!simulate`, the scenario harness and `!why` (#485) read the flag; a check at import or in the regression lists any handler that still sends a hand-written ❌.
- **#584 Forgiving command forms.** Places where a natural phrasing is refused today accept it, each probed on main:
  - Count and direction in either order: `!ent move hero right 2` answered "Count must be followed by a direction", and `!ent push gob 2 right` failed the other way, the two verbs taking opposite orders. A number and a direction can't be confused, so both verbs take both orders.
  - Passive options on either side of the formula: `!passive add hero p1 on_var_changed "1" target=hp` answered "Invalid passive formula: Syntax error", because options must come before the quoted formula.
  - `;` in a macro: `!macro set m1 "a ; b ; c"` was saved as one line and failed at run time with a usage error about `;`. `!macro set` can split on `;` as `!batch` does.
  - Bare list forms: `!ent status hero` answered "missing arguments"; it can list, as its list action does.
  - Bare `!undo`: it prints the help page; it can mean `!undo command 1`, with the usual confirmation thresholds.
- **#585 Replacing a container asks first.** A typed write that would destroy a container's contents says so before doing it. In a probe `!ent set_var hero inventory.potion.amount 2` then `!ent set_var hero inventory.potion 5` replaced the whole potion item with the number 5 and answered only "hero vars.inventory.potion = 5" (CLAUDE.md lists it as a known authoring trap). When `!ent set_var` / `set_vars` would replace a dict holding keys with a plain value (or delete it), the command refuses and names what would be lost ("inventory.potion holds 1 key: amount"), and `confirm` goes ahead. Formulas keep writing freely; this is for typed commands, where the mistake happens.
- **#586 Lines that show the real change.** The damage and healing lines report what happened to the unit, with the surplus marked. Today a unit at 21/30 hp healed for 50 printed "💚 Ogre receives 50 healing" and logged "received 50 healing" while its hp rose by 9; a 10-hp unit hit for 100 printed "takes 100 damage". The record holds both numbers (`healing_received: 50`, `healing_gained: 9`); the default lines print only the first. Two summary placeholders, `{effective}` (what the pools really changed by) and `{surplus}` (overkill or overheal), and new default formats: "takes 10 damage (90 overkill)", "gains 9 hp (41 over)". The event-log lines follow; a GM who prefers the offered amount keeps `{amounts}`.
- **#588 Failures inside a batch or macro name their line.** When one line of several fails, the reply says which. Today a three-line macro whose second line failed printed "Damaged hero by 1. / ❌ Entity 'nobody' not found. / Damaged hero by 1.", and in a forty-line setup macro matching an error to its line by eye is slow. A failed line's reply is prefixed with its position and text: "❌ line 2 (`!ent hp nobody -1`): Entity 'nobody' not found." Successful lines stay as they are. The same for `!batch`, `!foreach` (with the unit it was on) and `!run` (with the file's line number).
- **#589 Arguments for `!run` files.** `!run setup.txt level=3 theme=ice` fills `$level` and `$theme` in the file. Today `!run <file>` takes a file name only, so one setup script on disk can't serve two encounters. The file's lines go through the macro substitution (`_macro_args`: positional, named, `${name:default}`) before they run; a `$name` with no argument given stays as written, as in macros, so `!foreach`'s `$id` keeps working inside a file.
- **#594 One shape grammar.** The shape words of `!map preview` (burst, cone, line, rect, and `from=<eid>` from #275) work wherever a command takes an area. Today each command has its own: `!zone fill lava 4 4 6 5` is a rectangle and nothing else, `!zone add lava rect 4 4 6 5` is refused, `!reveal_fog` uses `at <x> <y> <r>` and `around <eid> <r>`, and `!reveal_fog blue at 5 5` (radius forgotten) answered "coords/radius must be integers". One shared parser for a shape argument, used by `!map preview`, `!zone fill`, `!tile place_area` / `fill` and `!reveal_fog`; the old `!reveal_fog` spellings stay as aliases. Every shape goes through the builders the formulas use, so a previewed cone and a filled cone cover the same cells.
- **#595 Clearing one cell or a rectangle of tiles.** `!tile clear 2 3` clears that cell; `!tile clear rect 1 1 4 4` a block. Today `!tile clear 2 3` answered "Unexpected 3 — usage: `!tile clear [confirm]`": the command only wipes every tile of the map, and a single cell is cleared key by key with `!tile del` or with `tile_clear(x, y)`. `!tile clear` takes a cell or a shape (#594); the whole-map form becomes `!tile clear all confirm`, so the dangerous one takes more typing.
- **#597 Short answers for unknown subcommands.** A mistyped subcommand gets two or three suggestions, including ones under another command. Today `!status remove hero burn` printed the entire `!status` help, about fifty lines over several Discord messages, while the form wanted was `!ent status hero remove burn`. The reply names the closest subcommands of the same root by spelling and by argument shape, plus entries from a small table of cross-references ("removing a status from a unit: `!ent status <id> remove <name>` or `!status dispel <eid> <token>`"), ending with "`!help status` lists all". Reuses the scoring of #562.
- **#598 More forgiving forms.** Four more natural phrasings refused today:
  - Keywords for positional numbers: `!status apply gob burn level=2 duration=3` answered "level must be an integer". Any optional positional slot named in a usage string can also be given as `name=value`.
  - `!zone hook lava on_enter "..."`: refused, since hooks want `!zone hook add lava ...` while every other zone edit puts the name first (`!zone glyph lava ~`, `!zone set lava ...`). A hook given without `add` sets it.
  - `!mount hero gob`: refused for the missing slot although the goblin has one slot. A vehicle with a single slot needs no slot name.
  - `!prefab save camp map=deck`: a save with no rectangle takes the whole map.
- **#599 Reacting to an action by name.** `reacts_to: "name:slash"` offers the reaction when the action called slash is used. Today `reacts_to` is a tag filter: a dodge written with `reacts_to: slash`, meaning the action's name, let the attack through with no window and `!react` said none was open (`!action info` does say "to actions tagged slash", so the data was read as designed; the mismatch is easy to make). `reacts_to` accepts `name:<action>` tokens beside tags, as status tokens tell a bare name from `tag:<x>`; a bare word matching no action tag anywhere in the match but an action's name gets a ⚠️ at write time suggesting the `name:` form.
- **#601 Hook results in plain words.** A ⚙️ line prints a hook's result the way the rest of the bot prints values. Today stepping a goblin into lava printed "⚙️ zone lava hook on_enter fired for gob → {'hp': 3}", a raw Python dict in a line players read. The result goes through the formatter replies already use: a dict as `hp 3`, a list joined with commas, a unit id as its name marker, None as nothing; the same for passive and status-hook lines.
- **#602 What ticks next.** `!status next <eid>` tells a player what their statuses will do at the coming turn boundaries. Today `!ent info` shows level and duration, and when each status acts is in the definition's `tick_when`, which players read only as raw JSON. A read-only listing, no formula run: per status, when it ticks relative to the unit's turn ("burn ×2: ticks at the end of your turn; 3 left") and whether it skips the turn, read through `status_field` (`tick_when`, `skips_turn`, level, duration; level entries count). Presented statuses under a disguise are used for a fooled viewer.
- **#603 `!help hook <name>`.** A help page per hook: when it fires, on whom, in what order, and which bindings it supplies. Today `!help` covers commands, rules, tags, sizes and functions; what `on_damage_taken` binds (attacker, defender, damage_taken, ...) or that `self` in a var hook is the unit whose turn it is lives in rule descriptions and the source. A table keyed by hook name, shown by `!help hook [name]` and searched by `!help find`; the same table the formula index needs to resolve names (#568, pitfall 5) and the binding lint needs (#427), with a drift guard against the fire sites.
- **#604 Teleporting a group in formation.** `!ent tp group:party 3 3 deck` moves the whole group and keeps its shape. Today group moves exist for walks and tp rejects a group (`_ENT_GROUP_REJECTING_SUBS`), so moving a party to another map is one teleport per member with each cell worked out by hand. The first member lands on the given cell and the others keep their offsets from it; all or nothing (every body must fit, members' own cells ignored for each other), across maps through `move_to_map`; exit, enter and stop hooks fire per unit as for single teleports.
- **#611 A default stash for transforms.** A transform always leaves a way back. Today `!ent transform gob hero` turned the goblin into a copy of the hero and stored nothing, since the stash path is optional, and `!ent revert gob` then answered "missing arguments": it needs the path, and none exists. A rule `transform_default_stash` names the path used when none is given (default `__forms`, a list so transforms still stack; empty turns it off); `!ent revert <id>` with no path takes the newest entry, and `!ent forms <id>` lists what a unit can revert.
- **#613 Rolling out a partial's edit.** After editing a partial template, one command shows who still has the old version and one updates them. Today `!template refs <unit>` reads one unit and `!template resync <unit>` updates one, and `!template delete sword_basic` went through while a unit still held a sword_basic, with no mention of it.
  - `!template refs all [partial]` lists the units of the channel's match whose copy differs from the library's current version.
  - `!template resync all <partial> [preview]` resyncs them as one undo step, all or nothing, each keeping its own changes as the single-unit form does.
  - `!template delete` names the live holders in loaded matches before it goes (the usedby listing of #370).

## Medium (a subsystem slice)

### Still open

- **#16 Path cost and reachable cells.**
  - `path_cost(eid, x, y)` and `reachable_cells(eid, budget)`;
  - a per-tile `move_cost` field;
  - a diagonal-cost rule.
  - All of these are footprint-aware.
- **#17 `!ent move <id> to <x> <y>`.** Walks the route #16 computes step by step, firing every per-step hook. The GM or player invokes it; the engine only finds the path.
- **#18 Team relations matrix.** Allied / neutral / hostile between pairs of teams, feeding `is_hostile`, the relation filters and disguise checks.
  - Raised and parked by the user: the topic may need a proper team object (a `Team` class holding a team's data, stances and vision settings); today a team is the bare `team` var text plus `team_data`. Settle that first. Today: `is_hostile` = different teams, `ally` filter = `same_team`, `is_attackable` = hostile unless `friendlyfire`; fog vision, fog memory, reveals, `pov_own_team_visible`, disguises and `team_data_visibility own` compare one team name.
  - Open questions (my recommendation after each):
    1. Where a stance lives: each team's data under a checked key (`!team set red relations.blue ally`, checked in `team_set` like `tags`), or its own match field. Rec: team data.
    2. One-way or mutual: one-way (`is_hostile(a, b)` reads a's team's stance toward b's; allows a betrayal), plus `!team relation <a> <b> [<stance>] [mutual]`. Rec: one-way.
    3. Which stances: a fixed `ally` / `neutral` / `hostile`, a team's stance toward itself always "same team". Rec: fixed.
    4. Pairs with no entry: rule `team_default_relation` (default hostile, today's behaviour) and `teamless_relation` (default hostile).
    5. Predicates: `is_same_team` unchanged, `is_hostile` = stance hostile, new `is_ally` (same team or ally) and `team_relation(a, b)` (same / ally / neutral / hostile); the `ally` filter widens to allied teams, `same_team` stays strict, a new `neutral` filter. Open: should `ally` widen?
    6. Attackable: replace `friendlyfire` with a word-list rule `attackable_relations` (default `neutral,hostile`; friendly fire on = `same,ally,neutral,hostile`).
    7. Disguises: a word-list rule `disguise_seen_by` (default `same`, today's behaviour; `same,ally` widens it).
    8. `team_data_visibility` gains an `allies` mode; under `own` a team can't read another's stance toward it (a secret betrayal stays secret). Rec: yes.
    9. A `team_relation_changed` event on the bus (team, other, old, new)? A watcher composes it. Rec: no, unless wanted.
    10. Per-unit stances (a charmed unit hostile to its own team): left out, a charm changes the team var. Rec: defer.
- **#19 Shared vision between allied teams.** Allies see what each other sees under fog. Needs #18.
  - Parked with #18 (same note on a team object). Open questions:
    1. Who shares: a per-team list in team data, `shares_vision_with: [blue]` (red shows blue what red sees), plus a rule `allied_vision` (default on) sharing wherever the stance is ally. One-way, direct only (red -> blue -> green doesn't give green red's view).
    2. What's shared: current sight and active reveals. Fog memory: the receiver remembers what it sees through the shared view from then on, without inheriting the ally's older explored cells.
    3. Allied units and stealth: a sharing team's units show to the receiver like its own units, past `entity_visibility_condition`; a word-list rule `pov_always_visible` (default `same,vision`) replacing `pov_own_team_visible`.
    4. Formula reads: `team_sees_cell`, `team_sees_entity` and `map_seen_by` include shared vision, with `shared=False` for a team's own units only; `can_see` and `visible_entities` stay per unit.
- **#20 Vision cones.** A `fog_vision_arc` var combined with facing limits what a unit sees.
- **#21 Cover primitive.** `cover_between(a, b)` gives the fraction of the line between two units that is obstructed, footprint-aware.
- **#22 Action cooldowns and charges.** Fields on an action that count uses and turns; the engine refuses the action while on cooldown or out of charges.
  - See #24's sketch, which covers #22, #23 and #24 together.
  - With action tags (#234) shipped: Solis shock forbids every action with a cooldown, so an action with a cooldown could carry a derived tag (`cooldown`) a status can forbid; until then the GM tags those actions by hand.
- **#23 Declarative action targeting.** `range`, `requires_los` and `relation` fields on an action, checked before the body runs, with the refusal reason reported.
  - See #24's sketch, which covers #22, #23 and #24 together.
- **#24 Action economy.** `turn_resources` pools (actions, bonus actions, movement) refilled at turn start, spent by actions' `cost` programs (#36).
  - A sketch written against the combat systems (docs/combat_systems_fit.md):
    - Action fields:
      - `kind`: standard, free, reaction, charged, combo;
      - `cost`: a formula program like `prepare` / `body`, run by the engine at #36's cost point (user call; no `{stamina: 8}` shorthand). "Pay the rest in hp" (FCS1 and NCS Lethal Determination, NCS Revenant) is written in it;
      - `cooldown` in the owner's own turns;
      - `uses_per_turn`;
      - `range` with a metric, `requires_los`, `relation`;
      - `requires`: a condition with a message (melee against flyers, disarmed, blind);
      - `charge`: `{turns, cancel_at_damage, release}`.
    - A `turn_resources` rule (`{"standard": 1, "moves": "var:moves"}`) refilled at turn start:
      - modifiers can raise a resource (FCS1 and NCS Efficiency: +1 standard action; Solis Agility: +1 move);
      - cooldowns can carry a modifier (Solis Efficiency: every cooldown 1 turn shorter).
    - Walking spends `moves` when a rule says so, only on the unit's own turn and only for walks; pushes never do (D3).
    - The engine checks all of it before the body runs and refuses with the reason ("Hunter1 already used its standard action", "dodge: 2 turns of cooldown left"). `!action list <eid>` marks what is available now.
    - Charged actions count down on the owner's turns, skipped ones included (D8, L12).
  - From the combat systems:
    - Combo groups: several different actions of one combo type in a turn, each at most once (Solis combo actions, e.g. foraging and attack combos).
    - Follow-ups need no field here: a follow-up is a #36 reaction on its holder's own action (`self_trigger: true`, an after window), or any other composition (user call). NCS Follow Up Strike (one light attack after any melee attack); FCS1 Riposte (an attack at half cost after blocking); FCS1 Unrelenting Assault (attack again after a blocked or dodged attack).
    - Resource modifiers through the modifier fold:
      - FCS1 and NCS Efficiency: +1 standard action;
      - Solis Agility status: +1 standard move;
      - Solis Slow: −2 sprint speed, and −1 move for units with more than one.
    - Sprint as a resource exchange: Solis spends the standard action for 2 extra tiles; FCS1 pays stamina per extra tile ("cost 5 stamina per extra tile"). FCS1's "prevents the use of ranged attacks that turn" becomes a `this_turn` flag (#243).
- **#26 Ready / delay a turn.** `!turn delay <after_eid>` moves the current unit to act after another unit this round.
- **#28 Player-controlled units.** A `controllers` list on a unit; a player's commands on their own unit skip approval.
  - A sketch written against the combat systems (docs/combat_systems_fit.md):
    - A `controllers` list per unit (user ids; `!ent control <eid> <@player ...> | clear`). Saved with the unit, stripped from templates and transfers like other identity data.
    - Gate: a player's command whose unit argument they control skips approval when the command is on a `player_unit_commands` rule list (default: `action`, `ent move`, `ent face`, `mount`). Everything else still queues for approval.
    - `caller()` in formulas returns the typing user's id (None for the host's CLI); `controls(user, eid)` tests it. An action can then refuse "not your unit" by itself.
    - Possession: the status's add hook sets `controllers` from its data (#227 `data=`), and the removal hook restores them.
    - The per-player access overrides that shipped (`@<user id>` keys) remain for whole commands.
  - From the combat systems:
    - Temporary control: possession sets the unit's `controllers` for as long as it lasts and restores them on removal.
    - Summons follow their summoner's controllers (#223). FCS1: "summoned units are controlled by the user"; FCS1 Umbral Clone (a copy of the user, controlled by the user).
    - A `!ent control` listing for hosts; a spectator role that can read and never act (#190).
- **#30 ASCII map import.** A pasted text block plus a glyph → tile template (or unit template) legend builds a board.
- **#32 Procedural generation.** `noise(x, y, seed)` plus room and cave helpers that write tiles.
- **#33 Hidden rolls.** `!roll secret ...` shows the result to hosts only.
  - Formula rolls are hidden with `say(text, private=True)` (#215); this entry is the typed `!roll secret`.
- **#34 Session recap export.** The event log as markdown, filtered by POV, for posting a session summary.
- **#138 Tethers.** `tether(a, b, max_dist, mode=drag|block)` keeps two units within a distance, either dragging the follower along or refusing the move. For grapples, leashes, chained prisoners and harpoons.
- **#139 Movement layers.**
  - A per-unit `layer` (ground / air / burrow) with occupancy tracked per layer.
  - A flyer passes over a ground unit or shares its cell, and block conditions can read the layer.
  - Elevation (#37) is separate: that one is terrain height.
- **#141 User-defined commands.** `!cmd def <name> <usage> <body>` registers a command with named arguments, an access level and help text that runs an action or macro body. Players type `!attack goblin`, and the usage string feeds the stray-word check and Tab completion.
- **#143 Team-based turns.** A `turn_mode` rule (`unit` | `team`). On a team's turn any of its units can act; `!turn next` passes to the next side, and hooks fire per side turn and per unit. Wesnoth / XCOM style.
- **#144 Phase-based rounds.** A `round_phases` list (e.g. move, then act). `!turn next` cycles every unit through phase 1 before phase 2, with phase hooks.
- **#146 "What changed" view.** `!map changes` marks the units and cells that changed since the start of the current turn (or since you last looked), for players catching up on Discord.
- **#147 Image-map labels.** Optional HP bars, name labels and a row of status icons on sprites, each toggled by a rule.
- **#148 Live combat log channel.** Event-log lines posted as they happen to a channel bound as `log`, filtered by that channel's POV.
- **#150 Independent status stacks.** A definition mode where each application is its own instance with its own duration (three bleeds on three timers). Today one instance per name carries a level.
  - A sketch written against the combat systems (docs/combat_systems_fit.md):
    - `stack: independent`: each application keeps its own level and timer. `stack_effect` decides what the stacks do together:
      - `highest`: only the highest level acts, while every level keeps its own duration (Solis);
      - `sum`: the levels add up;
      - `each`: each stack ticks on its own (three bleeds on three timers).
    - Removes D4. `status_get` and #222's display read the effective level and list the stacks.
  - From the combat systems:
    - Showing the stacks in #222's status line, in Solis's own format: "(5 lvl1 burn, 2 lvl2 burn)".
    - Solis Plague and Steelmoss: damage that grows each turn while the stack lives, resets when it expires and is kept when the status is reapplied; a per-stack counter field holds the growth.
  - Open questions raised, then parked by the user (my recommendation after each):
    1. Storage: one instance per name holding a `stacks` list (each stack its own level, duration, fields); the top-level `level` is the engine-kept effective level, so everything keyed by name keeps working. Rec: this, over separate `burn#2` instances.
    2. Top-level `duration` = the longest stack's; direct writes of level / duration on a stacked instance refused with a pointer to `stack=` (or: applied to every stack).
    3. Two knobs: `stack_level` (highest / sum / newest) and `stack_tick` (once / each, binding `stack_level` / `stack_id`), each with a default rule; or the sketch's single `stack_effect`. Rec: two (sum ticking once, highest ticking per stack).
    4. Decay stays in the GM's tick: a counter outside a per-stack tick changes every stack, inside one only that stack; a stack at 0 drops, the last one removes the status (`expired`).
    5. Re-application: `stack_merge` none (a new stack, default) / same_level (refresh that stack to the longer duration; Solis).
    6. Cap: `max_stacks` with a rule `status_stack_overflow` (replace_shortest default / replace_oldest / refuse); `max_level` caps each stack and the summed level.
    7. Data: the application's level, duration and seed data go on the new stack; definition `data` is copied into the instance once and shared.
    8. Surface: `status_stacks(eid, name)` (stack dicts with a stable `id`), `stack=<id>` on status_get / status_set / counters, `status_stack_remove`, a `stacks` view on `!ent status` / `!status`.
    9. Hooks stay status-level (on_added: first stack, on_removed: last, on_changed: a stack added / dropped or the effective level changed, with `stack_id` / `stack_event`), or new on_stack_added / on_stack_removed.
    10. Resistance folds each application into its stack; dispel removes all stacks; transfer moves all; a zone grant owns one stack (marker on the stack).
    11. Display: a `{stacks}` placeholder from a `status_stack_format` rule (default `{duration} lvl{level}`, joined ", ").
    12. Switching a live status's mode converts instances (one stack of its level and duration; back: the effective level and longest duration), firing on_changed.
- **#151 Named selectors.** `!selector def enemies "team!=red hp>0"`, usable as `!foreach @enemies ; ...` and as `select('enemies')` in formulas.
- **#152 Directional armor and coverage** (§8 combat layers). Per-part, per-side armor values and coverage %, with `armor_at(target, part, side)` and a mitigation helper the GM's damage formula calls. Primitives only.
- **#153 Vehicle follow-ups** (§8). Cover for riders inside a vehicle, a footprint per rider, and shared fuel or initiative between nested vehicles.
- **#155 GM test runner.** `!test <macro>` runs a macro full of `!assert` / `??` checks in a throwaway clone of the match and reports pass/fail. The scenario harness, for a GM's own rules.
- **#157 Edge-aware `side_hit`** (§8). For long bodies, the body edge an adjacent attacker touches decides the side hit.

### New

- **#189 Game time.**
  - `time_per_turn` / `time_per_round` rules, `game_time()`, and `!time advance 8h`.
  - Schedules and status durations can key on game time, for torches burning out, 10-minute spells and travel between fights.
  - The engine only counts time; what it means stays with the GM.
- **#190 Spectators** (the role part shipped: `!server perms` capabilities and `!host access ... role=`).
  - A spectator binding: a read-only channel with a POV that refuses every state-changing command outright, without queuing it.
  - Capabilities are a fixed set for now (user call); custom capability sets per role are open.
- **#191 Tiled map import.** A Tiled JSON map becomes tiles through a GID → tile-template legend, and its object layer becomes template spawns. Sprite users can build maps in a real editor.
- **#192 Optional var schemas.** A system can declare types and bounds (`mana: int, min 0`) and refuse writes of the wrong type. Today a string stored where a number belongs fails later and quietly inside a formula.
- **#193 Partial references in statuses and passives** (§8; you said "discuss after partial templates shipped"). Today only var subtrees can reference partials; statuses and passives stay owned by the template.
- **#194 Formula-aware Tab completion.** Inside a quoted formula (`!eval "...`, passive bodies, `!func def`) it completes function names, `entity[` ids and var paths.
- **#195 Revive from a template** (§8). `revive(eid, template=...)` regrows lost parts and restores stats from a template.
- **#196 Deep-illusion disguise** (§8). Today a disguise changes only what outsiders see. This one also fools their targeting and queries (relation filters, `!find`, `nearest_entity`).
- **#208 Item-borne passives.**
  - Passive records inside item data (`equipped.ring.passives.regen = {when, formula, target?, scope?}`) fire as the unit's own passives while the item sits under a `passive_sources` root (default `equipped`; per-unit replace / add vars in the shape of `modifier_sources`). `source` is bound to the item's container, as in an item's action.
  - Equipping means moving the item into `equipped`, and unequipping ends it. No `!passive add` / `remove` bookkeeping, and a looted or transferred item takes its passive along.
  - Today modifiers (`modifier_sources`) and actions (`discover_actions` containers) are found in item data, while passives have to be copied onto the unit and taken off again. That breaks "what X does is stored in X" for items.
  - The game need: FCS1 Paladin Armor ("grants lvl2 regeneration status while worn") and Tournament Armor ("grants lvl2 resistance status while worn").
  - To settle: var-hook passives inside items (are `target` paths relative to the item?), partial templates (an `axe_common` partial carrying its passive), whether `equipped` stays one root shared with modifiers.
- **#209 Tracked unit references.**
  - A rule `unit_ref_paths` (var paths, `*` for any key: `pet, grappling, summoner, minions.*`) names vars that hold unit ids. The engine treats them like its own link fields:
    - removing the named unit clears the ref, and a list drops the entry;
    - a clone or copy remaps refs that point inside the copied subtree;
    - a template save stores them relative to the root, as part ids are;
    - a transfer strips refs to units left behind.
  - Probe on 836e3af: `hero.pet = summon(wolf)`, then `remove_entity(wolf)`, then a new, unrelated summon is minted `wolf` again, and `entity[entity[hero].pet].hp` reads the new wolf's 9. `!ent clone hero hero2` copies `pet = 'wolf'` too.
  - This is CLAUDE.md §5's dangling-relational-id class moved into GM data, where nothing guards it. A smaller first step: a `reuse_unit_ids` rule (off = a match never mints an id it has used before, as a corpse's id stays taken while the corpse lies).
  - Builds on it: #56 (status source), #138 (tethers), #223 (`summoned_by`), NCS Taunted (the taunter), FCS1 Grab (the carried unit).
- **#210 One coordinate grammar.**
  - Commands: every `<x> <y>` slot also takes one word `x,y` or `x,y,map`, and `@point` once #76 exists; `map=` stays.
  - Formulas: done in pass 46 (the document 2 pass 25 fixes, F21 / L15): every x, y pair takes a coordinate list in the x slot (`[x, y]` / `[x, y, map]`, `formula._coord_lists`), the summons take the map in the slot after the coordinates as `move_entity` and `summon_template` do, and `id_prefix` is keyword-only. Left: making every argument after the coordinates keyword-only (`summon_near(t, [3, 3, 'deck'], radius=2)`).
  - `!action ... map=<map>` names a location target's map since pass 46 (F22).
  - No command and no formula function takes arguments after its map slot: commands take `map=` anywhere, the spatial functions take `map` as a keyword, and the placement functions with a positional map slot have it last. So leaving the map out never needs a placeholder.
- **#211 Hook timing report.**
  - Each top-level command records the time spent in every passive, watcher, status tick, tile / zone hook and `!func` it ran (runtime-only, the last N commands per match). `!match perf [n]` lists the costliest by total and per-call time; `!owner perf` does the same across servers.
  - Document 2 timed an 18-deep `!func` recursion at 2.0 s, one `emit` at 5.4 s and a 60 s command (X4, X5). A GM whose bot stalls has no way to see which formula did it.
  - Pairs with #197: the work counter stops a runaway, this report names it.
- **#213 Starter systems.**
  - Two or three ready systems shipped with the bot as data, loadable on any server: status definitions, templates with actions, passives, tables, macros, a demo map.
  - Solis is a natural first one: its core is already built in `audit/combat/solis1.txt` and `solis2.txt`, and it is the smallest of the three systems.
  - The engine has no combat or inventory by design, so a new GM starts from an empty system. The scenarios show every piece on its own and none of them as a game. A starter system shows the composition, and doubles as a realistic board for the chaos harnesses.
  - Shape depends on #162 (system packs) or a bundled `!store load` save.
- **#227 Status application options.**
  - `source=` shipped with #25 (`status_source`, a tick's default attacker); `data=`, `cap=` and the definition fields stay open.
  - `status_apply(..., data=None, cap=None)`, and the same keys on `!status apply`:
    - `data` is merged into the instance before any add hook runs (L8: possession's new side, the taunter of a Taunted unit);
    - `cap` limits the resulting level for this application only (FCS1 weapons: "inflicts lvl 1 burn, can stack up to lvl 2"; the attack scales the cap by the attacker's damage buffs).
  - Definition fields:
    - `cancels: <token>` with `cancel_mode: remove_both | subtract` (Solis: a burn on a frozen unit removes the freeze and doesn't apply; FCS1: burn and freeze subtract levels and the remainder lands);
  - Removes D7 and most of D16. FCS1's "only on ≥ 1 damage" stays in the GM's attack, where the damage is known.
  - From the combat systems:
    - FCS1 "statuses from attacks only apply if the target takes at least 1 point of damage" as an `on_damage_only` flag that the GM's damage step reads.
- **#231 Rules handbook export.**
  - `!match handbook [file]` / `!system handbook <name> [file]` writes a Markdown file to the saves folder (or posts it in parts):
    - status definitions with a `description` data field, levels, ticks and stacking in words;
    - actions on templates with their descriptions, costs and ranges;
    - tables;
    - the rules that differ from the defaults.
  - The user's groups keep their rules as documents (like the three evaluated here) and implement them by hand; this shows the bot's actual version next to the document and keeps the two in step.
  - Reuses #202's function table for formulas it quotes.
- **#239 Kill credit and damage attribution.**
  - The `killer` binding on `on_death` shipped with #25; the per-pair tally stays open, with #240.
  - The engine keeps a per-match tally of damage dealt and taken per pair of units.
  - The game need: FCS1 rewards that depend on who killed whom.
    - A base human gains 40% of their stamina capacity per kill; Bloodthirst adds more.
    - Executioner refills its user after a kill.
    - "You only receive rewards for the enemies that actually get killed."
  - Today `on_death` binds no attacker (`dupact.out`: `attacker=None`). An action can credit itself by checking the target's hp after its hit; a kill by damage over time can't be credited.
- **#240 Battle report.**
  - `!match report`, and automatically after `declare_winner`: per unit damage dealt and taken, kills, statuses applied, turns taken, fled or dead.
  - A GM `report_formula` per unit adds the system's own lines: FCS1 XP (an enemy's XP value scaled by comparing the gear tier with the match tier), FCS1 loot rolls, Solis value points ("fleeing hunters won't get any value points").
  - Builds on #239 and #241.
- **#262 Side phases (IGOUGO).** As in Wesnoth, Fire Emblem, Advance Wars, XCOM: a whole team takes its phase, its player activating its units in any order, then the next team. Team grouping already keeps a side together and orders sides; the missing part is a phase in which any of the side's units still to act can take the turn (`!turn next <eid>`, or a unit acting starting its turn), with turn hooks per unit and a `on_phase_start` / `on_phase_end` pair per side. Alternating activation (Kill Team, Descent, Lancer) is the variant where a side activates one unit and play passes to the other side; design both together.
- **#249 Definitions as text.**
  - `!match export defs` prints a match's definitions (status definitions, global and team passives, functions, tables, tile templates, watchers, macros, aliases) as one JSON block. `!match import defs <json>` loads them into another match, on any server, through the same checks as each command.
  - This is how `!template import` already moves units between servers, and a step toward #162 system packs without the packaging.

- **#290 Status-granting tiles.** `grants` on a tile template, settled by the zone-grant machinery. Terrain bonuses as in Wesnoth: a forest tile gives a status carrying a defence modifier while a unit stands on it. Today ground needs enter / exit hooks that every teleport, death and revive has to keep in step.
- **#291 Typed action arguments.** Builds on #228: `args: {dest: {type: location}, ally: {type: entity}}`, parsed, coerced and view-checked like the main target. For "throw an ally to a cell" and "swap two units". Today a second target is raw text with no existence or view check.
- **#292 Action area preview.** `!action <eid> <name> <target> preview` marks the cells and lists the units the action's `affects` gives for that target; nothing runs and no dice roll. Complements #238, which reports refusals.
- **#293 Timed entries in the turn order.** `!turn event add <name> <initiative> <formula> [once]`: a named entry that fires when the order reaches it, then the order moves on. For lair actions, "at initiative 10 the lava rises", reinforcement waves. It appears in the forecast; today the GM parks an invisible unit somewhere.
- **#294 Team seats.** `!team seat <team> <player ...>` assigns users to sides. A seated player's commands on their own side's units skip approval for the commands on a `seat_commands` rule list, and the turn line can name the side's players. A per-side cousin of #28's per-unit controllers, sharing its gate.
- **#295 Per-match rule layer.** `!match rule set <rule> <value>`: a last layer after the system chain, saved with the match, undoable, shown by `!help rule`. For "this one fight has friendly fire"; today that takes a child system per match.
- **#296 Conditional tags.** A system library `tag_rules` (`bloodied: "entity[self].hp * 2 <= entity[self].max_hp"`): a unit carries the tag while the expression holds, so every tag filter (action immunity, `!find tag:`, modifiers, step filters) reacts to state. Cost is one evaluation per rule per unit read, so it needs a per-command memo and an owner limit.
- **#297 Bags.** Tables drawn without replacement: `!table bag <name>`, `table_draw`, `table_left`, `table_reset`, on the match RNG, saved and undone with the match. For card initiative, loot decks and chit-pull activation.
- **#298 Campaign vars.** `!server var set|get|del|list` and `server_var_get` in formulas: values shared by every match of the server (a day counter, faction reputation). Reads go into the journal the way prefab reads do.
- **#299 Match archive.** `!match archive <id>` / `unarchive`: a finished match leaves memory and the lists, keeps its files and refuses commands until brought back.
- **#300 `!match doctor`.** The chaos harness's invariants run on the live match: dangling ids, units off the map or on voids, statuses without a definition, actions calling missing functions, units standing on cells that block them, the turn pointer. `fix` repairs the ones with a single right answer.
- **#301 Live roster and turn-order boards.** `!list autoupdate` and `!turn autoupdate`: a Discord message that refreshes as map boards do. #248 appends a roster to replies; this is the pinned version.
- **#302 Recipe book.** `!help recipe [topic]`: the composed patterns (channeling, charge-up, summon lifetime, a reach-aura attack, corpse decay, a one-way door) as searchable entries, each backed by a scenario in the suite so it keeps working. It helps a GM by showing how to build common composite mechanics, the companion to the "compose it from primitives" rule.
- **#304 Neighbour-aware tile sprites.** A tile template's `sprite_auto: wall` picks `wall_<mask>` by which neighbours share the template, so walls and coasts join up. Graphics only.
- **#336 `!simulate compare`.** Two commands run on the same seeds, reported side by side with the difference. For balancing weapon A against weapon B.
- **#337 Seat bonuses.** A mount slot's `modifiers` and `grants_unit_tags` apply to its rider while seated (a gunner's seat, a howdah's cover). Slots carry actions today and nothing else.
- **#338 Zone-granted actions.** A zone's or definition's actions are offered to units in or next to it (a reach field), discovered like seat actions, with `source` bound to the zone. The lever's "pull" and the chest's "open" then live in the object. Pairs with #311.
- **#339 Destructible objects.** A zone with pool vars takes damage packets (`deal_damage` on a zone, steps and hooks included, an `on_destroyed` hook). Completes the object story with #311 and #338; today a breakable door has to be a unit.
- **#340 Suppressed statuses.** A definition's `suspended_while` expression or `suspended_by` token: the status stays on the unit while its modifiers, restrictions, grants and ticks pause (an antimagic field, a silence). A field decides whether the duration keeps counting.
- **#341 Sighting events.** `on_unit_sighted` fires when a unit first enters a team's sight under fog, and a rule `sighting_stops_movement` ends a walk at that step, as Wesnoth does. Cost is a sight check per step, charged to `sight_check_limit`.
- **#342 Revealed private vars.** `reveal_var(eid, path, team)` / `!ent reveal`: that team reads the private var from then on, stored per unit like `actions_seen`. For Analyze and Identify skills.
- **#343 Banded display of private numbers.** A rule `private_var_bands` (`hp: healthy:75%, wounded:40%, critical`): a reader without access sees the band, as sizes show a category. Gives "bloodied" without exposing the number.
- **#344 Status-held team change.** A status field `team_while`: the bearer counts as that team while it holds the status and returns on removal, the newest winning when two overlap. Charm and possession appear in all three of the group's combat systems; hooks that stash and restore the team by hand break on overlap.
- **#346 Scenario coverage report.** `run_scenarios.py --coverage` lists the subcommands, rules and formula functions no scenario exercises. Points the next audit pass at untested code.
- **#347 Benchmark suite.** `tools/bench.py` times fixed boards (1,000 units on 150×150, 10,000 units) per command against a stored baseline and fails on a slowdown. Keeps the #197 gains from eroding.
- **#373 Bracket access for zones, teams and maps.** `zone[trap3].armed = false`, `team[red].gold += 5`, `map[deck].alarm`: the `entity[...]` syntax for the other stores, compiled onto their get / set functions. Pairs with the object ideas (#311, #338, #339).
- **#374 Initiative through modifiers.** A rule making the sort read `apply_mods` over the turn-order var, with a status change that alters it re-sorting as an initiative write does. Haste and slow become modifier records that end with their status; the twin of #95.
- **#375 Edge walls.** Tile data `walls: [up, right]`: thin walls on a cell's edges, blocking a step and sight across that edge from both sides. Today a one-way door through `move_dir` needs both cells authored, and sight has no edge concept.
- **#376 The event log records who saw each entry.** The teams able to see the unit when the entry was written, as `actions_seen` records them. It gives players a `!log` and is the base for the recap (#34), the log channel (#148) and "what changed" (#146). Today `!log` turns host-only once anything is hidden.
- **#377 Action forecast text.** An action field `forecast` (a read-only expression with the target bound) shown by `!action info <eid> <name> <target>`: "65% to hit, 8-14 fire". GM-authored and RNG-free, so players may read it while #238 keeps real outcomes with hosts. Uses #361.
- **#378 Command deadline.** An owner limit `command_time_limit_seconds`, tested at the points limits are already charged (loop iterations, cells, hook fires, macro steps); past it the command stops and rolls back with a note. A wall-clock stand-in for the deferred per-server work budget; a stopped command writes no journal entry, so replays stay consistent.
- **#379 Save format version.** Every file the storage layer writes carries a format number and loads run numbered migrations; the existing fix-ups (`LEGACY_IDS_OK`, `legacy_template_fixes`) become the first ones. For the public rollout.
- **#380 Reply snapshots.** `run_scenarios.py --snapshot` writes every scenario's full replies; `--diff` lists the replies a change altered. It catches drift no `??` asserts, and a PR can state "these 14 replies changed".
- **#381 Record a session as a scenario.** CLI `!as record <file>` writes the typed commands in scenario format with `??` lines taken from the observed replies. A bug found while testing after a merge becomes a scenario in one step.
- **#382 Parallel scenario runs.** `-j N`; scenarios already run in separate temp folders, and the suite runs twice per change (plain and under `FCS_SPATIAL_CHECK=1`).
- **#411 Status-borne passives.** Passive records in a status definition (`passives: {thorns: {when: on_damage_taken, formula: ...}}`) fire as the bearer's own while it holds the status, level entries included. A definition today has a tick and three lifecycle hooks, so Thorns, a counter stance or "heal on kill" need a global passive that tests `status_has` on every unit. The twin of #208.
- **#412 Status-granted actions.** A status's actions are offered while it is held: Rage adds Frenzy, Grappled adds Break Free. Discovery reads unit vars and mount slots today.
- **#413 Restore one unit.** `!history restore <save> unit=<eid>` and `!undo command N unit=<eid>` put one unit's body back from a snapshot and leave the rest of the match alone. For a mistake noticed five commands late.
- **#414 Parameter sweeps in `!simulate`.** `vary=hero.str:10..20` runs n times per value and prints one row per value, for balance curves.
- **#415 Dormant maps.** A map var marks a map inactive: its units leave the turn order and their ticks and schedules pause until it wakes. The hold of a ship while the fight is on deck.
- **#416 Prefab anchors.** Named points inside a prefab (`anchors: {door_n: [3, 0]}`) and `align=door_n@x,y` on a stamp, so rooms snap together at their doors; `prefab_anchor(name, anchor)` reads one. Builds toward #32.
- **#417 Lazy matches.** A server loads each match on its first use; today a server's folder loads whole. Fits #197 and #299.
- **#418 Slim CLAUDE.md.** Move §7's subsystem reference into `docs/reference/<subsystem>.md`, read when a task touches that subsystem, and keep philosophy, process, the map and the bug classes in CLAUDE.md. Every session reads its 4,471 lines before the first edit.
- **#442 Window prompts reach the reactors.** A reaction window's option lines also post in the channels bound with the reacting unit's team view. They print where the action was typed, so in team-channel play the defender never sees the prompt.
- **#443 Preview a system edit.** `preview` on `!system set` and the library editors: per match, what the edit would change (renamed vars, re-sorted orders, size sweeps, capped statuses). `!dry` refuses server-wide edits, and a system edit reaches every match at once.
- **#444 Pin a match's rules.** `!match pin system`: the match keeps a copy of its system chain and stops following live edits until `unpin`, the rule twin of `template_pinning`. A fight in progress stays as it began while the admin edits the system for the next one.
- **#445 Part actions on the body.** A body's `!action list` includes its living parts' actions (`via tail`), runnable through the body with `source` bound to the part; a destroyed part's actions drop out. Today the player has to know and type the part's id.
- **#446 Directional auras.** `shape=cone|front|rear arc=<deg>` and an offset on `!zone anchor`, re-stamped on moves and facing changes. A shield wall's front arc, a lighthouse beam, a guard's watched cone; an aura is a radius today.
- **#448 Player-facing lines audit.** Move the engine-worded lines that remain into format rules: turn skipped, turn passes, reaction window options, block refusals, move replies. Follows "when in doubt, gamerule".
- **#449 Persona lines on Discord.** With a rule on, `say()` lines about a unit post under the unit's display name and sprite through a channel webhook. For tables that roleplay in the same channel; it can serve as a built-in stand-in for Tupperbox.
- **#450 Compressed history and journal files.** Undo snapshots and journal segments written gzipped (JSON shrinks roughly tenfold). The storage layer writes plain JSON, and undo history is the largest share of a server's quota.
- **#478 Observer passives.** A unit's own passive with `watch=allies|enemies|any [within=<r>]` fires when the hook happens to another unit, with `self` the owner and `subject` that unit. "When an ally dies I gain rage" then lives in the unit; today a team or global passive has to find the holders. Indexed per hook name, so units without one cost nothing.
- **#479 Aura occlusion.** An option on anchored zones: the aura covers only the cells its anchor has a clear line to, so a bless aura or a torch stops at walls. Auras are stamped by radius alone today; #107 covers the burst builders.
- **#480 Simulate under other rules.** `rules=<rule>:<value>,...` on `!simulate` and `!dry` runs under temporary rule values, so a rule choice is compared by outcome before anyone edits the system.
- **#481 Personal aliases for commands.** `!my alias <name> <command ...>` per user, resolved before the gate and still gated. With the `me` token (#400) a player types `!slash gob1` on a phone instead of the full `!action`.
- **#482 RNG commitments.** `!match rng commit` publishes a hash of the seed, and `reveal` at the end lets players check through the journal replay that every roll came from it. For duels between the user's groups, where the host is also a player.
- **#501 Roster views.** Today `!list [full] [as=<team>]` (`_list_cmd_impl`) prints every visible unit in turn order through one format, `entity_line_format` (`_entity_line`); a multi-map match groups through `_roster_groups`. One format has to serve combat, inventory and initiative reading at once.
  - Surface: rule `roster_views` (view name → line format, a MERGE rule so a child system adds views to its parent's); `!list view=<name> [sort=<var path>[:desc]]`, the same two options on `!state`; rule `roster_default_view` (empty = `entity_line_format`).
  - Behaviour: a view's format takes every entity_line_format placeholder and passes the same validation (`_FORMAT_RULE_CHECKS`). `sort=` is a stable sort inside each group (map and team headings kept); a unit without a number at the path goes last; without `sort=` the order stays the turn order. The → marker, the part and mount suffixes and the Dead: section stay. Sorting reads through the command's VAR_READER, so a private var counts as missing for a reader who may not read it and the order reveals nothing. An unknown view is refused with the list of views.
  - Where: `_list_cmd_impl`, `_entity_line` (a format argument), `_roster_groups`, the `!state` handler, completion.py (a view slot).
  - Scenarios: a view changes the columns; `sort=hp:desc` with one unit lacking the var; a team-view channel sorting by a private var (order unchanged for hidden values); a two-map match keeps its headings; a 2×2 unit with a located part keeps its suffix.
  - Open question: whether a view may carry its own corpse line (`{line: ..., corpse: ...}`) or corpses always use `corpse_line_format`; the plain string form first is the recommendation.
- **#502 `in=<zone>` on spawns.** Today `!template spawn <name> <x> <y> [near=] [count=] [map=]`; count goes through `_spawn_many` (the free cells nearest (x, y), all or nothing). A random spot in a zone works for one unit through `$(random_free_cell(...))` and has no form for several distinct spots.
  - Surface: `!template spawn <name> in=<zone> [count=<n>] [fit=body|anchor]`, x and y left out.
  - Behaviour: each copy takes a random free cell where its whole body fits, drawn from `m.formula_rng()` (the journal's RNG positions cover it). `fit=body` (default): every body cell inside the zone; `fit=anchor`: the anchor inside and the body on the map. The zone's own map is the spawn map; a `map=` naming another is refused; an aura counts as a zone. All or nothing, worded like `_spawn_many`: "Only 1 of 3 ogre fit in zone north_gate; spawned none".
  - Where: the spawn branch of `!template`, `_spawn_many` (a cell-picker argument), `formula._random_free_cell` for the shared fit logic, the `annotate_sub("template", "spawn")` usage.
  - Scenarios: five 1×1 units in a 3×3 zone on five distinct cells inside it; a 2×2 body with count=2 in a 3×3 zone refused whole; `fit=anchor` lets the body overhang; a zone on another map than `map=`; a fixed `random_seed` gives the same cells twice.
  - Open question: a formula twin `summon_template_in(name, zone, count=1)`, recommended in the same PR for command-vs-formula parity.
- **#503 Pending notice for hosts.** Today a queued request is announced once, in the requester's channel; a host working elsewhere sees it only by typing `!pending`.
  - Surface: rule `pending_request_notice` (off / count / list, default count).
  - Behaviour: after a host's command on the match in a channel without a team view, the reply ends with "🕓 2 requests waiting (!pending)"; `list` adds each request's id, requester and command. It sits beside the held reminders in `CommandRegistry._run` with their test (`_acts_as_host`, `_view_pov(...) is None`, not `!as`). It shows once per change of the queue: a runtime dict keyed by (match, channel, host) remembers the ids shown, outside match state, undo and the journal, like the private-var reminder cooldown. `expire_pending_requests` runs first; `!pending`, `!approve`, `!deny` and `!cancel` print no notice.
  - Scenarios: a player queues from a team channel and the host's next command in the GM channel carries the notice once; a second host command carries none; a new request brings it back; `!as wait` past the expiry removes it; a host typing in a team-view channel gets none.
- **#504 Fork from the past.** Today `MatchManager.clone_match` copies the present state, resets owner / co-hosts, bindings, cameras, pending requests and pause, and gives the copy a fresh RNG seed, uid and an empty history.
  - Surface: `!match clone <new_id> [name] at=<save name | command:N | turn:N | round:N>`.
  - Behaviour: the snapshot is resolved the way `!history restore` and `!undo` resolve theirs, and its `Snapshot.state` becomes the copy's state. A snapshot has no rules, so the copy takes its rules from the source's current system through `apply_system_rules`, following `_restore_snapshot` (var-name sync, the stub-rules marker). Everything `clone_match` resets is reset; the source, its history and journal stay untouched. A missing or unreadable snapshot is refused with the reason.
  - Where: `clone_match` (a `state=` argument), the clone branch of `!match`, `_restore_snapshot` as the model; `_require_match_creator` already guards it.
  - Scenarios: save, play three turns, `!match clone fork at=start` gives a round-1 board while the source keeps its turn; `at=command:2`; an unknown save; the fork rolls from its own seed; a fork of a match with template pins keeps them.
- **#505 System glossary.** Tags have descriptions (`tag_vocabulary`), statuses and actions have description fields, and system terms ("bloodied", "standard action") have no home.
  - Surface: a `glossary` library in SYSTEM_LIBRARIES (term → text), read live like `tag_vocabulary` and `var_labels`; `!system glossary list|set|remove <system> [<term> <text ...>]` (administrator or the `systems` capability, modelled on `_system_labels`); `!help term [<word>]` for everyone, modelled on `_help_tag` (no word lists the terms of the channel's system).
  - Behaviour: lookup ignores case and a miss offers close names; `!help find` searches terms and texts; Tab completes terms (a term slot). Limits beside `VAR_LABEL_MAX`: a term up to 40 characters, a text of one line up to 500. Inheritance, hides, `!system unset glossary:<term>`, info / diff, server undo and the audit come with SYSTEM_LIBRARIES. The handbook export (#231) prints the glossary first.
  - Scenarios: set and read a term; a child system overrides one term and hides another; a player reads it in a channel with no match; an over-long text refused; `!system undo` takes an edit back.
- **#506 Sprite sheets.** Today `SpriteLoader.get(key)` loads `<key>.png` from the server's sprites folder, then the shared one (PNG only, `_safe_path`, cached by path and mtime, shrunk past `_SPRITE_MAX_SIDE`); each sprite is its own file.
  - Surface: a sprite key `sheet:<file>:<col>,<row>` (1-based) or `sheet:<file>:<name>`, wherever a sprite key goes.
  - Behaviour: a sidecar `<file>.sheet.json` holds `{"cell": [w, h], "margin": 0, "spacing": 0, "names": {"orc": [3, 2]}}`, a server-side file like the sprites. The loader caches the sheet and each cropped cell, refreshed when either file changes. A sheet keeps its full size under its own larger limit (the 2048 px shrink would move every cell); each cell obeys the normal one. A cell outside the sheet, a missing or malformed sidecar gives None and the renderer falls back to the glyph.
  - Where: `SpriteLoader` only; check that the places validating or printing keys (`!map scene list`, any key character check) accept `:` and `,`.
  - Testing: the harness can't see pixels, so a direct test builds a 2×2 sheet in a temp folder, renders through `render_scene_png` and compares the cell's pixels.
- **#508 Part-borne modifiers and tags.** Today `_raw_modifier_records` gathers from statuses, match vars, map vars, team data, the `modifiers` var and the `modifier_sources` roots, and `unit_tag_sources` passes tags from a root down to its parts. Nothing flows from a part up to its body, so a crippled leg needs hooks to slow the creature.
  - Surface: a part var `body_modifiers` (a bundle shaped like `modifiers`); a part var `body_tags` (a checked tag list, engine tags refused, like `grants_unit_tags`); a rider var `vehicle_modifiers`, with a slot field `rider_modifiers` (default true) to switch a seat off.
  - Behaviour: every living part of a root's subtree (hp above 0 or indestructible, the `__part_destroyed` latch clear) contributes its records sourced `part:<part id>.<key>` and its tags sourced `part:<id>`, to the root only. A destroyed part contributes nothing until healed above 0; a detached part stops; the read is live, no hook. `value` and `condition` run with self = the body root and `part` bound. A mounted rider (hidden ones included) contributes to its vehicle. `!mod show` lists them with their source, and `{tags_sourced}` names the part.
  - Where: `_raw_modifier_records`, `unit_tag_sources`, `checked_tag_vars`, `Match._children_index` for the body walk (a per-node subtree scan cost 12 s on a large board, X18).
  - Scenarios: a leg with `body_modifiers` speed +2, destroyed, then healed; a nested part (claw on an arm) reaching the root; a glued part and a region part; a detached part; a driver's `vehicle_modifiers` and a seat with `rider_modifiers: false`; a `body_tags` entry feeding `immune_to_actions_with_tag`.
  - Open question: self = the root with `part` bound (recommended), or self = the part.
- **#511 Per-map rule overrides.** Today gameplay rules are read as `self.rules.get(...)` with no map involved; display settings already have the pattern (`Match.map_setting(mp, setting)`: a map var, else the rule).
  - Surface: a map var holding rule → value, named by a new MAP_VAR_NAME_RULES entry (`map_rules_var`, default `rules`); `!map rule set|unset|list [<rule> [<value>]] [map=<map>]` (host, strict map resolution); a whitelist `MAP_OVERRIDABLE_RULES`.
  - Behaviour: `Match.map_rule(mp, key)` returns the map's value, else `self.rules[key]`, and every read site of a whitelisted rule moves to it (each has a map in scope). A value passes the rule's own schema check, a formula value its mode; a rule outside the whitelist is refused with the list. Layer order with #295: system chain, match layer, then the map. `!help rule` adds the map's value as a source line; `!map cell ... for=` names a map override when it decided the answer. A drift guard at import fails the build when a whitelisted rule is read through `rules.get`.
  - Scenarios: diagonal movement off on one map and on in the other, tried with a 2×2 body; `los_corner_mode` differing between a deck and the sea; a non-whitelisted rule refused; an invalid value refused; a map copy carrying its overrides; undo of `!map rule set`.
  - Open question: the first whitelist; the proposal is allow_diagonal_movement, allow_diagonal_facing, block_walk / tp / push / swap, los_corner_mode, fog_los, fog_range_mode, friendlyfire and the tile / zone / corpse block and opaque condition rules, adding others as games ask.
- **#513 Owner switch for a command.** Today a command found exploitable on a public bot stays reachable on every server until a redeploy.
  - Surface: `!owner disable <command> [<sub>] [server=<key>|all] [reason ...]`, `!owner enable <command> [<sub>] [server=...]`, `!owner disabled`.
  - Behaviour: stored in `data/bot_settings.json` beside the ceilings and owner limits, per server key or `*`, with the reason. Tested in both `CommandRegistry.run` and `dispatch_no_snapshot`, after alias resolution and next to the administrator check, so batch, macro, foreach and `!run` lines, action `cmd()` lines, approved requests and held commands all meet it. The refusal: "`!ent tp` is switched off by the bot owner (reason)." `owner`, `help` and `whoami` can't be disabled.
  - Scenarios: a disabled subcommand refused when typed, inside a batch, through an alias, through an action's `cmd()` and through an approved request; `server=` scoping with `!as server`; the switch surviving `!as restart`; enabling again.
  - Open questions: whether the bot owner bypasses it (recommended: no, the goal is to close the code path); whether `!match replay` runs an entry whose command is switched off (recommended: refuse it and report the entry); a twin for formula functions (`!owner disable function <name>`) for an exploit through `$()` or a formula, recommended as a follow-up.
- **#529 Unit and cell choices.** `choose_unit(prompt, ids)` and `choose_cell(prompt, cells)` beside `choose()`, which takes any list and matches the typed answer as text. `choose_unit` shows each option by the name the asker's view may read and accepts an id or that name. `choose_cell` accepts `x,y`, and on surfaces that draw maps marks the options with the preview highlight and numbers them. Both validate the answer and replay through `answer=` like `choose()`.
- **#530 Question windows.** A body asks other units' players a question and waits: `ask_units(prompt, units, options)`. Today `choose()` asks only the player who ran the action, and reaction windows, the one place other players answer, are tied to reactions. The exchange stack that runs reaction windows gets a second window kind: the body stops at the call, each named unit's player answers with `!answer <eid> <option>`, the picks stay hidden from other teams until all are in (or the window expires with a default), and the body resumes with a dict of unit → answer, following the react-point machinery (the step re-runs from its start with the recorded answers). Uses: "each target drops prone or takes the damage", sealed bids, a defender choosing which part takes the hit. Open question: the default when a window expires (the first option, a named `default=`, or failing the action); a required `default=` is recommended.
- **#531 Rectangular bodies that turn.** A 3×1 cart facing right becomes 1×3 when it faces up. Today a footprint is fixed vars; in a probe a 3×1 unit faced up and kept its 3×1 body, and only a prefab stamp's rotation swaps width and height. A per-unit var `__turns_with_facing` and a rule for the default (off): a facing change between a horizontal and a vertical direction swaps the two footprint vars through the normal fit check, pivoting on a rule-chosen cell (anchor or centre); a turn that doesn't fit is refused naming the blocking cell. Located parts keep their offsets rotated, riders and auras re-stamp. The rectangle-only slice of #39. Open question: the pivot; the centre is recommended, which keeps a long body in place.
- **#563 Layer-aware status writes.** The other stacking modes and direct edits act on a layered status layer by layer. Today refresh, add_level, extend, replace, a direct write of level or duration, a counter set and a transfer onto a holder collapse a layered status to the level and longest duration it shows (burn 3 for 2 plus burn 1 for 5 becomes burn 3 for 5; §8, "acceptable for now, a known gap"). Each write gets a rule for layers: extend lengthens every layer; add_level raises the top layer; refresh and replace act on the layer at the applied level; a counter set on duration sets the longest layer and trims the rest to it; a transfer carries all layers and merges them into the holder's through the layer front. Each is a small, separately testable case. Open question: the per-mode rules above are a proposal; each is a design choice for the user.
- **#564 Deep undo through the journal.** `!undo journal <n>` rewinds further back than the snapshots reach. Today command autosaves cover the last 10 turns (at most 500 commands); the journal usually holds far more, and the only use of it is an administrator replaying an exported file into a new match. The command rebuilds the state at journal entry n by replaying the current segment from its start up to that entry (the machinery `!match replay` has) and puts it in place as a restore does (rules from the system, bindings per `undo_channel_bindings_mode`, the queue carried). It asks for `confirm` with the entry's command and round, and snapshots the present first so the rewind itself can be undone. A divergence during the rebuild stops it and changes nothing.
- **#566 Unit identity beyond ids.** Every unit gets a hidden permanent uid, and the engine's own links check it. Today an id is the only identity, so a link by id latches onto whoever holds the id later (§8: a suspended aura resumes around a different unit reusing its old anchor's id). The uid comes from a saved match counter (so replays agree), stays with the corpse and the revived unit, and is fresh for a clone, a copy or a new unit under a reused id. Aura anchors, status `__source`, queued extra turns, schedules and reaction records store the uid beside the id and treat a mismatch as "that unit is gone"; `uid(eid)` reads it for GM data wanting the same guarantee. The foundation #209's tracked references need.
- **#567 Team vision kept between commands.** A team's sight set survives from one command to the next and is updated by what changed. Today vision is memoised for one render pass, so every board refresh after every command recomputes each viewing team's sight (§8 lists "vision caching beyond one render" under later). The unit index already announces every move, footprint change and removal to listeners; a per-team cache listens, and also drops on opacity changes (tile, zone and void edits) and fog rule changes. `FCS_SPATIAL_CHECK=1` compares every cached answer with a fresh computation, as for the other caches. Worth building only after measuring: first time a board refresh on a large fogged map with the benchmark suite (#347).
- **#614 What happens when.** `!match upcoming` lists the scheduled future in one place. Today the pieces live in separate commands (`!schedule list`, `!remind list`, the extra-turn queue in `!turn`), and temporary fog reveals have no listing at all. One timeline ordered by due turn or round: schedules, reminders, queued extra turns, reveals that expire, and zone and tile lifetimes once #117 exists, each line saying what and when ("round 4: sched_1", "in 2 turns: reminder 1"). Hosts see everything in a channel without a team view; players see the public reminders and their own team's reveals. The future-facing companion of `!match waiting` (#525).
- **#615 Maps from formulas.** `map_create(name, w, h)`, `map_create(name, prefab=...)` and `map_delete(name)`. Today the formula list has map reads (`maps`, `map_of`, `map_carrier`, `inner_maps`, `outer_map`) and map vars but no way to make or remove a map, so an action can move a unit to another map only if it already exists. The functions call what `!map new`, `!map new from=<prefab>` and `!map delete` call, with the same checks (`match_cell_limit`, name rules, `map_delete_mode`, the prefab's system test), and return the map's name, minted on a clash as zone names are. With the `__delete_when_empty` map var this gives instanced interiors: stepping through a portal creates the pocket room from a prefab, moves the party in, and the room disappears when the last unit leaves. Journal replays stay exact because a prefab read is already recorded with its command. Open question: a per-command cap on maps created, in the style of `summon_event_limit`; one with an owner ceiling is recommended.

## Large (arcs of several PRs)

### Still open

- **#36 Reaction framework.** Built (`docs/damage_reactions_design.md` §1-§9, CLAUDE.md §7.23) except what its §13 lists as still open: a GM-called `react_point()` (actions a formula starts are react points already), Discord buttons for the options (with #42).
  - From the combat systems:
    - Windows, in the order an attack resolves:
      - before the hit: NCS First Strike, FCS1 Preemptive Strike, Counterspell, Protect, Emergency Consumable;
      - on the hit: block, dodge, NCS Smokescreen;
      - after the hit: NCS Fiery Counter (a Fire Elemental hits back after being hit), FCS1's "One with the Rot" boon (adjacent attackers catch Umbral Rot), FCS1's spiked shield hurting a blocked melee attacker.
    - Limits:
      - one reaction per attack (NCS);
      - one reaction until the unit's own next turn (Solis);
      - a bypass flag (FCS1 Preemptive Strike "bypasses the reactionary limit").
      - Settled: counted with `reactions_used` in each reaction's condition (design §8), no limit rule.
    - Redirects: a reaction may change the pending hit's target (FCS1 Protect: swap places with the attacked ally and take the hit at half damage).
    - Multi-hit attacks offer one reaction per hit (NCS "blocking is done individually per incoming hit").
- **#37 Elevation.** Per-cell height, high-ground LOS, `height_diff`, and a link to #16's `move_cost`.
- **#38 Light and darkness.** Light sources with radii, per-cell light levels, darkvision; fog and visibility read them.
- **#39 Arbitrary and rotating footprints.** L-shaped and other non-rectangular bodies, and bodies that rotate with facing.
- **#42 Discord interactive layer.** Select menus for `choose()`, action buttons, slash commands, turn pings and timers.
- **#43 History replay.** Stepping through snapshots on a board, or an animated GIF of a session.
- **#158 Campaign roster.** Characters stored at the server level, checked into a match and back out with their state (hp, inventory, XP vars). One roster serves a whole campaign of matches; it builds on the template library.
  - From the combat systems:
    - Levels and rebirth: a character keeps its gear and perks while its race template changes (NCS: "you can switch your race with rebirth"), with #251 recomputing the stats.
    - Gear tiers per match: FCS1 compares a character's highest item tier with the match tier when computing XP; the roster can record tiers per character.
- **#159 Per-player fog.** Vision per user: each player sees only what their own units see. Today vision is per team and per channel. Needs #28's controllers, plus DMs or per-player channels.
- **#160 Simultaneous turns (WEGO).** Players queue orders, and the GM resolves them all at once in an order a rule decides.
- **#161 Undo tree.** History branches: redoing after a different command keeps both timelines, with `!history branches` to switch. #66 is the small linear version.
- **#162 System packs.**
  - A versioned file bundling a system (with its parent chain), its status definitions, templates, partials, macros and tables.
  - Servers can share a ruleset and upgrade it.
  - Per-server storage (#41) and system status definitions, tables and macros (#140) have shipped.

### New

- **#263 Movement-triggered reactions.** A later trigger kind for #36's reactions: a unit's move (leaving or entering a reach, crossing a cell) opens a reaction window the way an action does, for opportunity attacks and overwatch. Left out of #36's first version (user call); a reach aura's `on_exit` hook calling `react_point` covers it until then.

- **#197 Scaling.**
  - Shipped: the spatial indexes (#154: units, zones and corpses by cell) and servers loaded on first use and unloaded when idle (`server_idle_unload_hours`).
  - Left: the per-server CPU work budget (§8).
  - Also shipped: serialization caches (a command serializes what it changed), diff-based undo snapshots (rounds too, `autosave_round_snapshot_mode`) and journal `state` entries, a tile time-hook index and fog stamping for full renders (1,000 units on a filled 150×150: ~300 ms per command before, ~15 ms after).
  - Left besides the budget: a formula namespace cache (approved, deferred: CLAUDE.md §8 "Large match state").
  - Also shipped: the per-command size sweep, watcher check and zone-grant check read only what changed (~140 ms per command at 10,000 units before, ~1 ms after).
- **#198 Play-by-post mode.**
  - Turn deadlines and a ping to the active unit's player (needs #28).
  - A digest of what happened since your last turn (#146).
  - A deadline rule that skips the turn when time runs out; the unit does nothing, so this stays clear of AI.
  - For Discord tables that play over days.
- **#252 Derived vars.**
  - A var declared as a formula (`max_hp := round(apply_mods(self, 'max_hp', 50 * level))`) whose value is computed when it is read.
  - The game need: NCS multiplies HP bonuses from class, armor and items, and Solis Endurance raises max hp by a percentage. A derived max hp would need no recomputation when gear changes.
  - Open questions: cycles between derived vars; whether var hooks fire when a derived value changes; how saves and undo store them; the cost per read. This is a larger shift than the rest of the list, since modifiers are pure queries today by design.

- **#306 Encounter library.** A saved bundle of a prefab, the watchers, macros, reminders and match vars that go with it, and a turn-order setup, stamped into a match in one command. Sits between prefabs (#31) and system packs (#162).
- **#348 Module split.** vtt_commands.py and logic.py are about 29,000 lines each; split them into packages by subsystem with pure moves. Every session reads less to find its way, and parallel PRs conflict less.
- **#385 Server isolation.** Each server's commands run in their own worker process, so one server's slow command no longer delays the others. Locks, storage and routing are per server already; this removes the shared event loop behind most of the limit rules and ceilings.
- **#509 Prefabs and templates carry inner maps** (medium to large). Today `capture_prefab` leaves a map carrier out with a ⚠️, so a ship with decks is rebuilt by hand in every match. The corpse of a wrecked map vehicle already stores a map inside a record (`maps`, re-attached by a revive, §7.8), which is the shape to reuse.
  - Surface: no new command. `!prefab save` keeps carriers; `!template save` of a carrier keeps its maps; `stamp_prefab`, `!prefab stamp`, `!template spawn` and `summon_template` rebuild them.
  - Prefab shape: a `maps` entry per carried map: its size, tiles, zones, voids and map vars, the saved id of its vehicle, the units and corpses on it (entity entries at offsets), and its own carried maps, recursively. Fog memory and reveals stay out.
  - Stamping: each map is created under its saved name, or a minted one on a clash (`deck`, `deck2`), and attached to the new id of its vehicle. One id map covers the whole stamp; inner zones are minted on a clash too; stored `[x, y, map]` lists naming a saved map follow its new name (as `_rename_map_coords` does). `rotate=` and `flip=` turn the outer rectangle; inner maps keep their orientation. `match_cell_limit` over the summed area, the owner limit `map_nesting_depth` and the unit cap are checked before the first unit spawns; a failure leaves nothing (`_rollback_match`). `_collect_definitions` scans the inner maps, and `check_prefab` validates them (bounds, voids, a vehicle that exists in the prefab, no carrying loop).
  - Templates: an entity template of a carrier stores its maps' terrain, zones and map vars.
  - Scenarios: save and stamp a ship with one deck and two crew; stamp it twice (minted map and zone names); a ship whose deck holds a boat carrying a map of its own; a stamp over the cell limit refused whole; a ship template spawned twice; a rotated stamp.
  - Open question: whether a carrier template also stores the units on its maps; recommended: terrain only for templates (a ship design), units for prefabs (a crewed ship).
- **#568 Formula index.** One analysis layer that parses every stored formula once and answers questions about all of them: where a thing is used, what a rename must touch, what a delete would break.
  - Today: only function calls are checked when a formula is stored (`validate_formula` / `validate_program`); a name in quotes (status, table, zone, template, event, a function name passed to `map_with`) is plain text until the formula runs. After a rename, `_mentions_note` searches each slot for the exact text `'old'` or `"old"` and lists up to 8 hits in a ⚠️, leaving the formulas as written. Probe: after `!status rename burn scorch`, a passive doing `status_apply(self, 'burn')` was listed and now applies an undefined status; a passive building the name (`'bu' + 'rn'`) was missed without notice; `say('burn')` would be a false hit. `!func del` deletes a function its callers still use, silently (only `!system lint` reports it later). Lint finds unknown functions by pattern-matching the compiler's error text (`_system_lint`); a prefab save finds used functions with a regex for `name(` (`prefab._used_funcs`).
  - Design: an index over every formula slot of a match and of its system. Per distinct formula text it keeps the parse tree and what the formula references: var paths read and written (per unit expression), functions called, and names in known argument positions (statuses, tables, zones, definitions, templates, events, tags); each formula function's signature says which argument is which kind of name. A derived cache in the style of the spatial index: never saved, marked dirty per slot at the store chokepoints (`write_var`, the status / tile / zone writers, the library editors), rebuilt lazily, reset by `_rollback_match` and an undo restore, and compared with a fresh build under `FCS_SPATIAL_CHECK=1`. Queries carry a scope: this match, its system's libraries, the server's templates and prefabs.
  - Pitfalls the build must handle:
    - Slot coverage: `_match_formula_slots` was written for lint; the complete list is the formula-text leak probe's (§5), adding modifier value / condition, mount slot condition / cost, colour formulas, `__death_condition`, schedule bodies, formula rules, macros, aliases, templates, prefabs. One shared slot registry for lint, the leak probe and the index, with a test planting a marker in every slot.
    - Computed names: `status_apply(self, name_var)` can't be resolved; every query result states how many dynamic references it couldn't follow.
    - Formulas inside formulas: `schedule(2, "...")`, `watch_add(...)`, `cmd('...')` carry a formula or a command as a string; the index recurses into them.
    - Command text: macro and alias lines contain formulas with `$1` holes; indexed as text with holes, results from them marked less certain.
    - `entity[name]`: whether `name` is a unit id, a local or a binding depends on the slot (§4); the index needs a table of the bindings each hook supplies, written once and drift-guarded.
    - Rewrites keep the GM's text: edit the stored source by character spans from the tree, never regenerate text from the tree; spans are computed on the text as stored after `normalize_body_source`.
    - Size: each distinct text parsed once; a thousand units from one template share their bodies.
    - Copies: a match holds copies of its system's libraries; snapshots and the journal hold old text and stay untouched. A rename reports which scopes it changed and which still hold the old name.
    - Privacy: queries are host tools; output in a team-view channel goes through `formula_text_visibility` and the var reader.
  - Build order: the slot registry and the index, read-only; where-used (#203) on it, checked against `_mentions_note` on every rename in the scenario suite; delete warnings, then rewriting renames (#204, #492), then the lints (#279, #427, #524).
  - Open question: whether a delete of something still used is refused or warned; a warning with the list and a `confirm` is recommended.
- **#570 Custom resolution pipelines.** The damage pipeline's machinery for kinds the GM declares: a to-hit bonus, a movement cost, a morale check.
  - Today: `damage.KINDS` holds two kinds, damage and healing. Each has a step library (`Match.damage_steps` / `healing_steps`, also system libraries), steps with side, order, filter and description (`normalize_step`, `ordered_steps`), the `step_` helpers reading `STEP_FRAME`, and `!damage explain`.
  - (The entry arrived cut off at this point; the design, surface and open questions are still to come.)

## Suggested groupings

- **Formula ergonomics:** #89, #194
- **Status bundle:** #114, #115, #116, #150
- **Zone bundle:** #117, #118, #119
- **Discord table play:** #128, #148, #182, then #28 and #198
- **Tactical combat:** #16 → #24 → #36
- **Scaling before a public rollout:** #177, #178, #197

From the combat-systems review (docs/combat_systems_fit.md):

- **Hosting turn-based combat systems:** #28 → #36 slice → #24 + #22 + #23 (with #243, #234, #233) → #227 + #232 → #150 → #222 → #220, #224, #225, #229, #217, #218, #219, #228, #226
- **Status bundle for the three systems:** #233, #234, #232, #227, #150, #222
- **After the battle:** #239 → #240, #241; #158 with #251
- **Discord table comfort:** #247, #248, #254, #255, #244, #245
- **Behaviour kept in its data:** #200, #208, #201
- **GM tooling:** #202, #203, #207, #211, #204, #231, #249, #250
- **Formula ergonomics:** #89, #194, #205, #210, #216, #217, #219, #228
- **Integrity of GM data:** #209, #210, #223
- **Before a public rollout:** #177, #178, #197, #206, #214
- **Onboarding:** #213, #202, #231

From the #264-#348 reviews:

- **Explaining the engine to a GM:** #277, #278, #279, #280, #281, #284, #300, #302, #318, #323, #333
- **Map looks:** #264, #288, #304, #335
- **Targeting:** #269, #270, #275, #286, #287, #291, #292, #311, #312, #313, #314
- **Objects as zones:** #311, #338, #339
- **Action limits:** #307, #308, #24
- **Status order and lifetime:** #266, #267, #290, #296, #309, #310, #340, #344
- **Private information:** #342, #343
- **Formula ergonomics (review):** #324, #325, #326, #272
- **Engine upkeep:** #346, #347, #348

From the #349-#385 review:

- **Explaining the engine:** #365, #366, #367, #368, #369, #370, #371
- **Formula language:** #349, #350, #351, #352, #353, #354, #373
- **Macros and sweeps:** #355, #363
- **Actions and objects:** #356, #357, #358, #364
- **Telling players the numbers:** #359, #360, #361, #362, #377
- **Turn order:** #374
- **Map geometry:** #375
- **Player-readable history:** #376
- **Before a public rollout:** #378, #379, #385
- **Test tooling:** #380, #381, #382, #410

From the #386-#418 review:

- **Hooks:** #386, #387, #388, #389, #390
- **Formula twins of commands:** #391, #392, #393, #394, #395, #396, #397
- **Approvals and players:** #398, #399, #400, #401
- **Looks and reads:** #402, #403, #404, #405, #406, #407, #408
- **Statuses that carry behaviour:** #411, #412
- **Prefabs:** #409, #416
- **GM tooling:** #413, #414, #415
- **Scaling and upkeep:** #417, #418

From the #419-#450 review (probed against the bot):

- **Rules and lines:** #419, #420, #421, #422, #448, #449
- **Explaining the engine:** #424, #425, #426, #427, #428
- **Hosting tools:** #429, #430, #443, #444
- **Formulas and actions:** #431, #432, #433, #434, #435, #445
- **Objects and containers:** #436, #437, #438, #439, #446
- **Safety and bookkeeping:** #440, #441, #450
- **Team-channel play:** #442

From the #451-#482 review:

- **Selectors and sweeps:** #451, #452, #453, #454, #455, #456
- **Formula helpers:** #457, #458, #459, #460
- **Passives:** #461, #462, #463, #478
- **Table play:** #464, #465, #466, #467, #468, #469, #470, #481
- **Maps and reads:** #471, #472, #473, #474, #475, #476, #479
- **Tooling:** #477, #480, #482

From the #483-#513 review (#501 and later come with a build sketch and the open questions):

- **Catching silent mistakes:** #483, #484, #485, #498
- **Explain tools:** #486, #487
- **Formulas and hooks:** #488, #489, #490, #491, #492, #493
- **Rules as data:** #494, #495, #497, #511
- **Table comfort:** #499, #500, #501, #503, #505
- **Building encounters:** #502, #504, #509
- **Looks:** #506
- **Bodies:** #508
- **Running a public bot:** #513

From the #515-#531 review:

- **Lines players read:** #515, #520, #526
- **Actions and choices:** #516, #519, #529, #530
- **Modifiers:** #517
- **Fog:** #518
- **Building encounters:** #521, #522, #531
- **Hosting tools:** #524, #525, #527, #528

From the #534-#550 review:

- **First steps of a new GM:** #534, #537, #538
- **Reads:** #535, #539, #540, #550
- **Team-channel play:** #543, #546
- **Surviving a restart:** #544, #545
- **Test tooling:** #547

From the #552-#567 review (#555, #556, #563, #566 and #567 answer items in CLAUDE.md §8):

- **Selectors and targets:** #552, #560
- **Modifiers:** #558
- **Privacy:** #553
- **Systems:** #555
- **Placement and hooks:** #556, #557, #559
- **Command wording:** #561, #562
- **Statuses:** #563
- **Undo and identity:** #564, #566
- **Performance:** #567

From the #568-#570 review:

- **Formula tooling (one index under #203, #204, #279, #427, #492, #524):** #568
- **Resolution pipelines beyond damage and healing:** #570

From the #583-#590 review:

- **Command wording and safety:** #583, #584, #585, #588, #590
- **Lines players read:** #586, #587
- **Scripts:** #589

From the #594-#605 review:

- **Command wording:** #594, #595, #597, #598, #600
- **Reactions:** #599
- **Lines players read:** #601, #602
- **Explaining the engine:** #603
- **Moving and copying:** #604, #605

From the #610-#615 review:

- **Dice and tables:** #610
- **Transforms and templates:** #611, #613
- **The match at a glance:** #614
- **Maps from formulas:** #615
