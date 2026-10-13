# Multiple maps per match (#40) and nested maps (#164): agreed design

Status: design in discussion with the user; nothing is built yet. Each item
below was decided in chat unless it sits under "Open". Read this whole file
before starting either feature: every decision here has long-term
consequences, and the user made each one deliberately.

- **#40 Multiple maps per match.** A match holds any number of maps. Portals,
  floors and stairs are composed from tiles and the movement primitives
  (below); the engine has no portal system.
- **Terminology (user rule): "host" means only the user who runs a match
  as GM.** The unit that carries an inner map is the **map vehicle**
  (binding `map_vehicle`, names like `on_map_vehicle_death`). Never call it
  a host: `on_host_death` reads as "the GM died". (The mount rule that
  broke this was renamed to `mount_on_vehicle_death`.)
- **#164 Nested maps.** A map attached to a unit (a ship's deck, a building
  interior) that travels with it. Its discussion is postponed (see the end
  of this file); #40 comes first.

## 1. Model

- **Match and Map split.** Today `Match` fuses the match and its one map. A
  new `Map` class holds what is spatial:
  - grid size, tiles and zones;
  - fog memory (`explored`) and fog reveals;
  - background, borders, hidden layers;
  - the map display settings (coords, legend, render mode).

  `Match` keeps everything else: units, the turn order, rounds, rules,
  passives, definitions, functions, aliases, macros, tables, watchers, match
  vars, team data, schedules, the event log, hosts, access, approvals, RNG,
  the journal and undo history.
- **Units stay in one pool on the match**, each with a `map` field. Ids stay
  unique match-wide and non-spatial loops are unchanged; only spatial code
  filters by map. Parts, riders and segments take their root body's map.
- **One turn clock per match**, shared by all its maps. This was recommended
  and not contested; separate clocks are what separate matches are for.

## 2. Coordinates

- A coordinate is `[x, y]` on the context map, or `[x, y, map]` naming one.
  Every function and command that takes a cell accepts the third element.
  Results carry the map whenever it differs from the context map.
- **Spatial questions across two maps get "no relation", never an error:**
  - distance is None;
  - `has_los` / `can_see` are false;
  - area membership is false.

## 3. Which map a command or formula means

Resolution order (the "4-tier hierarchy"):

1. An explicit map: the third coordinate element, `map=`, or the extra
   argument on `!ent tp`.
2. The map of the unit the command or formula is about (`self`, the actor,
   the action's source, the unit a command names).
3. The match's only map, when it has one.
4. The channel's map. This is the map set with `!map view <name>` when one
   is set; otherwise it is the map the channel's board follows (§5).
   Buttons and browsing never change it.

**Strictness for broad commands.** With several maps, unit-less commands
that act broadly make the host name the map. These are tile fill / line /
set / clear, zone cell edits, `!reveal_fog` and `!tile place_random`. With
one map they stay as lenient as today. Commands fall into three kinds:

- **Per-map edits** take `map=<name>`, `map=a,b` or `map=all`. Each map gets
  its own share: a fill rectangle is clipped to each map's size, and a
  reveal applies per map.
- **Placing one thing** (`!ent add`, `!template spawn`, `!ent tp`) needs
  exactly one map. `map=all` is refused there. A unit the command names
  supplies its map (tier 2).
- **Reads** (`!map preview`, `!map cell`, `!dist` with coordinates) use the
  hierarchy and name the map in their reply.

Where strictness doesn't apply, or the host can't be asked, the hierarchy
decides. Mid-turn formulas have nobody to ask, so a formula with `self`
uses `self`'s map.

The refusal names the maps and the syntax, for example: "this match has 3
maps: add `map=<name>`, `map=a,b` or `map=all`". Tab completion offers map
names after `map=`. A GM's existing macros start refusing once a second map
exists; that is intended.

**Map shortcuts (decided).** `map_of('hero')` and `entity['hero'].map`
both read a unit's map. `map` joins `x` / `y` / `name` as a reserved var
path, so a unit can no longer hold a var named `map`. The reserved map
value `view` means the channel's viewed map (tier 4); a formula with no
channel refuses it. `view` and `all` can't be map names.

**Unit-less coordinate formula functions require a map** on a multi-map
match. These are `random_free_cell` / `random_cell` (which also take a list
or `all` and return `[x, y, map]`), `cells_in_rect` / `_area` / `_cone` /
`_line`, `cell_blocked`, `entities_at`, `cell_entity`, `nearest_cell`,
`in_bounds`, `tile_get` and the like.
- Without `map=` they work only when the match has exactly one map;
  otherwise they refuse for the missing argument. They don't fall back on
  `self`.
- The map argument also takes shortcuts: the channel's viewed map (tier 4)
  and "this unit's map". Section 10 lists the open details.

**Scattering across maps: N in total.** `!tile place_random N <kind>
map=all` (or a list of maps) places N in total, with every eligible cell
across the chosen maps equally likely. A big map therefore gets a bigger
share. An even distribution per map is a loop over the maps.

## 4. Moving between maps

- **No separate `move_to_map` primitive.** The placement verbs take the map:
  - `!ent tp <id> <x> <y> [map]`;
  - `move_entity(eid, x, y[, map])`, or a three-element coordinate;
  - `summon`, `summon_near`, `summon_template`, `!template spawn`;
  - `!ent add ... map=<name>`. Its usage already ends in an optional
    `[init]`, so a bare map word would be ambiguous there.
- **Step verbs stay on the unit's map:** `!ent move`, `push`, `pull`,
  `swap`, `move_step`. A path can't cross grids. A walk that ends on a
  stairs tile changes maps through the tile's `on_enter`.
- **A cross-map move is a full move.** It validates the whole footprint on
  the destination map. The whole body travels with it: parts, riders,
  segments. Enter hooks fire for the cells it lands on.
- **Hooks for map changes: both.** Every move fires `on_entity_moved` with
  `from_map` / `to_map` bindings, which are equal for an ordinary move. A
  move between maps also fires `on_entity_changed_map`, so a GM who only
  cares about map changes needs no from / to test.
- **Portals, stairs, teleporters and doors are composed:** a tile's
  `on_enter` calls the movement primitive. There is no portal system.

## 5. Channels, POV and boards

- **A channel binding carries POV only (a team's view).** A channel is never
  bound to a map. That would need one channel per team per map (40 channels
  for 4 teams × 10 maps), or constant rebinding.
- **Vision stays team-wide.** Per-unit vision is out of scope; the user may
  be open to it later.
- **Which map a team channel's board follows.** It shows the map of that
  team's current or most recent acting unit. With turn order red1, red2,
  blue1, blue2: during blue1's turn, the red board shows red2's map and the
  blue board shows blue1's. An omniscient (GM) channel follows whoever is
  acting. In detail:
  - **No unit of the team has acted yet this round:** follow the team's
    last actor from the previous round. Before the first turn ever, show
    the map the team's first unit is on, or the default map. This works the
    same under ATB.
  - **The followed unit changed maps after acting** (a portal at the end of
    its turn, being pulled through one): follow where it is now.
  - **The followed unit died or was removed:** keep showing its map until
    another unit of that team starts a turn.
  - **An acting unit the channel's POV can't see:** the board doesn't
    follow it. That would leak which map a hidden enemy is on.
- **Browsing.** Autoupdate boards can scroll between maps: ◀ ▶ buttons, or a
  select menu when there are many maps. The GUI uses a dropdown and the CLI
  `!map <name>`.
  - Browsing is temporary. The next turn change, any team's, returns the
    board to its followed map.
  - Browsing is display state on that board only. Nothing reads it, and
    several boards in one channel can show different maps.

## 6. Unseen maps and hidden maps

- **Rule `unseen_map_display`:** `show` / `blank_named` / `blank_unnamed` /
  `hidden`, default `blank_unnamed`. Each map can override it (for example
  `!map set <name> unseen=<mode>`, or `default` to use the rule).
- A map is **unseen** by a team when fog is on and the team has no visible
  cell there (no own units, no reveals) and no fog memory of it. The
  override applies only while the map is unseen; once seen, the map shows
  normally. A `hidden` vault appears in a team's list the moment one of its
  units steps in.
- **Blank boards** show a text, set by its own format rule, along the lines
  of "you have not seen this map yet". ASCII and graphics must agree, so
  the renderer parity pass applies.
- **`hidden` hides a map from everything a player can query:** board
  scrolling, `!map list`, `!map <name>`, `map=` arguments, completion and
  "did you mean" suggestions. Host-only commands state the facts. A host
  typing in a team's channel sees that team's view unless they add `full`,
  the same as unit hiding.
- **Counts.** `blank_unnamed` maps count in public map counts ("unexplored
  map, 3 of 7"). `hidden` maps never count publicly. Host-only commands
  count everything. Document this.
- **Refusal wording.** It must be identical for a hidden map and a
  nonexistent one, so the reply never proves the map exists:

  > Could not find a map named `NAME`. Either this map does not exist, or
  > this channel currently does not have permission to view it.

  **Unit lookups switch to the same two-sided wording** as part of this
  work, along the lines of "either this entity does not exist or is hidden
  from this channel's view". Today a hidden unit gets "Entity 'x' not
  found.", which reads as a confirmation that the unit doesn't exist.

## 7. Map settings, limits and map management

- **Map settings live in map vars, and gamerules stay per match at the
  finest.** The old fields assumed one match = one map, so a match
  couldn't have a fog-less outdoor area and a dark indoor dungeon. That
  now works:
  - Everything that may differ between maps becomes a **map var** on each
    map: the fog toggle, fog memory, background, borders, darkness, and
    so on.
  - The gamerules say **which map var means what** and **what its default
    is**, in the same way `hp_var` names a unit var and `default_entity_vars`
    fills missing ones: map-var-name rules and a default map vars rule.
  - Gamerules themselves are never overridden per map. A rule that would
    need to differ between maps becomes a map var named by a rule.
  - Formulas read map vars with `map_var(...)` (context map by default).
- **`match_cell_limit`:** a gamerule with an owner ceiling capping the
  summed area of every map in a match. Creating or resizing a map past it
  is refused with the current total named. `max_grid_dimension` still
  bounds each map.
- **`transfer_entity` formula** for cross-match moves (a mutating formula
  function). `!ent transfer` remains the command form. The journal stores
  both matches' changes as stored states.
- **Map names.** Every match starts with one map named `main`. Names follow
  the id rules and are unique within a match. Renaming a map rewrites
  everything that names it: unit `map` fields, stored `[x, y, map]` values
  in vars, channel views. Formulas that name it as text are listed in a
  ⚠️, as `!status rename` does.
- **Deleting a map with units on it:** a gamerule, `refuse` (default, the
  units are listed) or `kill`. Corpses and fog memory on the map go with
  it either way.
- **`!map copy <name> <new>`** copies terrain, zones and settings. Its usage
  hint says that it **does not copy units or corpses**.
- **Map size:** each map has its own size; `!map resize` takes `map=`, and
  a shrink follows `map_resize_shrink_mode` as today.
- **Roster** (`!state` / `!list`): units grouped by map, with each map's
  heading under the same unseen / hidden rules as boards. A gamerule picks
  the grouping order: team first then map, or map first then team.
- **Undo and journal cost.** Snapshots copy every map, so the "Large match
  state" item in CLAUDE.md §8 gets more pressing.

## 8. Build order

1. **Extract `Map`** with a single default map (`main`) and add the unit
   `map` field. Every spatial function takes its map explicitly. Behaviour
   stays identical, proven by the full regression and the replay
   differential. This step goes in its own PR, with no new features
   alongside.
2. **Multiple maps:**
   - `!map new/list/delete/resize/view/set`;
   - the map argument on the placement verbs;
   - per-map fog, boards and following;
   - unseen and hidden maps;
   - strictness and the refusal wording;
   - `random_*` with `map=`;
   - completion;
   - the POV filtering of map lists.
3. **Nested maps (#164).**

## 9. Tests (user requirement)

- **At least 20 scenarios** for #40, each on a different facet. They must
  include:
  - scatter N in total across maps;
  - the per-map loop;
  - a multi-tile unit crossing maps with its parts and riders;
  - strictness refusals and `map=all`;
  - unseen and hidden maps per mode, with counts and refusal wording;
  - board following, including the red/blue example;
  - the hierarchy tiers.
- **Beyond scenarios:**
  - the replay differential with journals spanning several maps;
  - the existence-oracle detector with hidden maps against nonexistent
    ones;
  - the POV leak detector;
  - renderer parity on blank boards;
  - the memory-equals-disk check.

## 10. Open

- **The rest of #164.** Postponed by the user. Points raised so far:
  - does a unit inside count as standing on the map vehicle's cells for outer
    distance (a rule)?
  - can the inside see the outer map (a rule, or per-map data)?
  - what happens to the interior's units when the map vehicle dies (below)?
  - could a mount slot point at an inner map?
- **Map vars:** the names of the map-var-name rules, and which display
  settings become map vars. The candidates: background, borders,
  coordinate rulers, legend, render mode (text / image), colour on/off,
  hidden layers.
- **`transfer_entity`:** which matches it may reach (the original #40
  wording: only matches with the same owner).
- **Roster grouping:** the rule's default order.
- **When a map vehicle dies** (user: it must vary per vehicle AND per
  occupant; mount deaths are to be revisited the same way later). The
  cases to cover:
  - a ground vehicle blows up: soldiers take damage, survivors are
    ejected, the dead die;
  - a spaceship breaks up: everyone dies except a vacuum-proof alien,
    which takes damage and is ejected;
  - no cheese: a boss lured into the players' vehicle must not die just
    because the vehicle is destroyed. Opposing teams can share an inner
    map.

  Agreed so far:
  1. **Per-occupant effect program**, run once per occupant in turn order
     with `self` = occupant and `map_vehicle` bound. The program comes from
     the occupant's var (`__map_vehicle_death_effect`), else the vehicle's
     effect var (name set by a gamerule), else a gamerule default (empty =
     nothing). **An occupant's effect replaces the vehicle's** (default
     precedence).
  2. **`on_map_vehicle_death`** fires per occupant (passives) after the
     effect.
  3. **Survivors** are ejected to the free cells nearest the vehicle's
     body, searching outward rings as dismount does. If nothing fits
     anywhere, the survivor stays on the inner map with a ⚠️, never killed
     silently.
  4. **The inner map's fate is configurable:**
     - **destroyed:** every occupant that wasn't ejected dies with it;
     - **kept as a wreck:** the map stays. A wreck formula (a program run on
       the map) can change it, for example scattering fire and debris
       tiles. A reserved map var (`__delete_when_empty`) deletes the wreck
       once no units are left in it.

  Still open: the names and defaults of the fate rule and the wreck formula
  rule, and whether corpses count as units for `__delete_when_empty`.
