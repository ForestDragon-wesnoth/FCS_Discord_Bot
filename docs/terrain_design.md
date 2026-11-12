# Ground tiles, object zones, moving zones and the corpse store: agreed design

Status: design agreed with the user (chat after #178, questions 1-18).
Built in three PRs, in the order of §6: PR 1 (§2) and PR 2 (§3, §4) built. Read this whole file before
touching tiles, zones or corpses.

## 1. Roles

- **A tile is ground.** One per cell, identified by its cell, holding that
  cell's own state (a filled wall keeps a separate dict per cell, so one
  segment can be knocked down). Tiles never move: a thing identified by
  its position has no identity left once it moves (user call, question
  12: no `tile_move` / `tile_step`).
- **A zone is an object, or anything that stacks or moves.** Identified by
  its name, holding shared state, any number per cell. A one-cell zone
  made from a definition is a placed object (a trap, a chest, a fire, a
  buzzsaw). Zones already carry definitions, movement hooks (boundary and
  per cell), time hooks, looks, block / opaque, tags, facing and grants.
- **Rule of thumb for GMs:** ground is a tile, anything standing on the
  ground is a zone. It goes into `!help tile`, `!help zone` and CLAUDE.md
  §7.12 (question 18).

## 2. Corpses leave tile data (PR 1)

- **Store.** Each map holds its corpses in `Map.corpses`: unit id -> the
  corpse record `{x, y, entity, died_round, groups?, parts?, maps?}`. `x` /
  `y` are the corpse's anchor cell, authoritative (the old tile key). Saved
  as the map's `corpses` dict. Tile data no longer holds corpses: `corpses`
  stops being a reserved tile key, tile edits stop keeping them, and
  `clear_tile_keep_corpses` goes.
- **One id, one unit, alive or dead** (user call, question 10): the
  `corpse_id_uniqueness` rule is removed and its true behaviour is always
  on. A corpse's id, and the ids of the body parts it holds, stay taken
  until the corpse is gone (revived, removed with `remove_corpse(id)` /
  `!ent remove <id>`, or dropped with its cell or map):
  spawns refuse them, minting skips them, a copy or transfer into another
  match remaps a collision, a prefab stamp remaps them. So the store is
  keyed by id across the match, and a revive always gets its id back.
- **Index.** `Map._corpse_ix` becomes an index built from the store: cell
  -> corpse ids (the anchor cell), part id -> corpse id. Every write goes
  through the store's methods (lay, take, move, change), which keep it and
  the serialization cache current; no stale entries to check on read.
- **Reads.** `!tile info` no longer lists corpses (question 11): `!map cell`
  lists everything on a cell, corpses included. `tile_get(x, y, 'corpses')`
  finds nothing; the corpse functions (`corpse_at`, `corpse_var`,
  `corpse_has`, `all_corpses`, ...) are the way in. Corpse visibility,
  blocking, rendering and the corpse targets are unchanged in behaviour.
- **Everything coordinate-bearing follows:** `!map resize` shifts corpse
  records, `!map rename` rewrites coordinates inside them as before, `!map
  copy` takes no corpses (as before), a deleted map drops its corpses,
  prefabs capture from and stamp into the store (the prefab shape keeps
  its `corpses` list), wrecked maps stay recorded in their corpse.
- **Undo diffs and the journal** diff a map's corpses by id, like zones by
  name (`match_history._MAP_SPEC`). Backwards compatibility is not kept
  (question 11): old saves holding corpses in tile data are not converted.

## 3. Zones as placed objects (PR 2)

- **Placing** (question 13): `!zone place <def> <x> <y> [name=] [map=]` and
  `zone_place(def, x, y, name=None, map=None)` (mutating) make a one-cell
  zone of a definition, named after it like unit ids (`buzzsaw`,
  `buzzsaw2`) unless `name=` is given. `zone_spawn` stays for any cell set.
- **Draw order** (question 14, a): both renderers order zones and tiles by
  one number. A zone's `sprite_layer` (its own field, else its
  definition's, a ZONE_LOOK_FIELD read live) else the `sprite_layer_zone`
  rule; a tile's `sprite_layer` data else `sprite_layer_tile`. In text maps
  each cell shows the highest-layer glyph; on a tie a tile is above a zone
  and a later-made zone above an earlier one, the order images already
  use. So a zone set above the tile layer draws over the ground in both.
- **Time-hook index:** zone time hooks read an index of the zones holding
  a hook of that kind (own or through the definition), kept by
  `zone_touch` and definition edits, so thousands of one-cell zones cost
  what tiles cost.
- **No cell read** (question 15, b): `zones_at` plus `zone_get` compose it.

## 4. Moving zones (PR 2)

- **Movers** (question 1, zone parts): `zone_move(name, dx, dy,
  hooks=True)` jumps; `zone_step(name, dir, n=1, hooks=True, face=False)`
  walks one cell at a time in a direction word (diagonals follow
  `allow_diagonal_movement`), stopping before a blocked step, and returns
  the steps taken (so a bouncing trap is `if zone_step(zone_name, d) ==
  0:` then turn around). A zone moves as one piece: if any of its cells
  would land on a blocked cell, that move or step doesn't happen.
  Commands: `!zone move <name> <dx> <dy> | <dir> [n]`. `zone_shift` keeps
  its clipping behaviour and fires nothing. An aura (anchored zone) can't
  be moved by hand: it follows its anchor.
- **Blocking** (question 3, a): off the map and void cells always block.
  Beyond that the rule `zone_move_block_condition` (an expression; empty =
  nothing else blocks) decides per cell entered, with `zone_name`,
  `zone_map`, `tile_x` / `tile_y` (the cell entered), `from_x` / `from_y`
  and `move_dir` bound; `self` = the acting unit. A zone's own `move_block`
  data, else its definition's, overrides the rule (a bool or an
  expression). Fail-open like the other block conditions. Units block only
  when the condition says so (`cell_entity(tile_x, tile_y) != ''`). The
  read `zone_can_move(name, dx, dy)` tests a move without making it.
  (Named `zone_*` where the questions said `terrain_*`: only zones move.)
- **Units under a moving zone** (question 4, user: a zone entering a
  unit's cell is treated like the unit moving into it): after each move or
  step, for every unit on the zone's map (alive, by its footprint cells,
  the same units the zone's movement hooks reach when they walk), cells
  newly covered fire `on_cell_enter`, cells no longer covered
  `on_cell_exit`; a unit covered now and not before fires `on_enter`, one
  covered before and not now `on_exit`. When the move or the last step
  ends, a unit the move newly covered fires `on_stop` and `on_cell_stop`
  on its covered cells, as a unit landing there would. The binding
  `zone_moved` is true in all of them (false when a unit moved), so a hook
  can tell them apart. `hooks=False` skips them for one call. Ghost firing:
  each unit is re-checked after every hook.
- **`on_moved`** (question 5): a zone hook fired once after each move and
  each step, with `zone_dx` / `zone_dy`, `move_dir` (None for a jump that
  isn't one step) and `zone_covered` (the units it covers now) bound
  (zone_-prefixed: a binding named `dx` would forbid that local in every
  GM formula). The buzzsaw's
  arrival damage lives there.
- **Facing** (question 6): `face=True` turns the zone to each step's
  direction (`zone_face`), so `sprites.<facing>` show where it heads.
- **Recursion:** moves set off by hooks count toward the same
  `move_hook_recursion_limit` / `move_hook_event_limit` chain as unit
  moves.
- **Not built** (question 7): units riding a moving zone (conveyors,
  platforms). A hook moves them with `move_entity` today.
- **Test:** a buzzsaw trap scenario (question 12): a one-cell zone that
  steps along a row, bounces off a wall, hits units it moves onto and
  units standing on it at their turn start, with a 2x2 unit.

## 5. Scenarios and guidance (PR 3)

- **Split** (question 17): ground stays tiles, re-themed where needed:
  walls, floors, water, mud, lava, pits, conveyor belts, a fixed one-way
  door. Objects become zones: traps, fire, landmines, spikes, chests and
  loot, the goal marker, the graveyard spawner, the hatch, campfires,
  beacons, springs, an openable door.
- Tile-machinery scenarios (template hook propagation, `place_area`,
  `fill_pattern`, reapply, the hook index) keep testing tiles with ground
  themes (the `flame` band becomes a lava band).
- Guidance as in §1.

## 6. Build order

1. Corpse store and id uniqueness (§2).
2. Zone placing, draw order, the time-hook index, moving zones (§3, §4).
3. The scenario rewrite and the guidance (§5).
