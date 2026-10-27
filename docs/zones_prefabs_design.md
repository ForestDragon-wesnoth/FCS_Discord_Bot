# Zone definitions, tags and prefabs (#31): agreed design

Status: design agreed with the user in chat; both PRs built (CLAUDE.md
§7.12 and §7.19 describe what shipped). Two PRs, in order:

1. **Zones and tiles** (§1-§4): zone definitions, zone and tile tags,
   minted zone names (`zone_spawn`).
2. **Prefabs** (§5-§9): saved board fragments stamped into a match. Prefabs
   build on PR 1: stamped zones mint their names the same way, and a
   prefab carries the zone definitions its zones use.

Read this whole file before working on either. Every decision below was
made in the discussion; ask before deviating.

## 1. Zone definitions

A zone definition describes a kind of zone once (`poison_cloud`), the way
a tile template describes a kind of tile and a status definition a kind of
status. A zone names its definition; checks that care about the kind read
the definition or the tags, and only "which specific zone" needs the
zone's own name.

- **Model: the tile-template pattern** (user call, for consistency within
  the "map feature with a definition" family):
  - **Live, the zone's own value winning:** hooks (boundary, per-cell and
    time hooks), glyph, color, sprite, legend and tags. Editing the
    definition reaches every zone that names it.
  - **Copied when the zone is made:** data. A zone's data is its own from
    then on; a later edit of the definition's data reaches only zones made
    afterwards. `block` and `opaque` are data keys (as today), so they are
    copied too, and read through to the definition's data when the zone's
    data lacks them (as a tile reads its template's).
  - Pushing data edits to existing zones and tiles (a `reapply` command)
    is a later idea (feature list).
- **What a definition holds:** data, hooks, glyph, color, sprite, legend,
  tags. Cells and the aura anchor stay per zone.
- **Where they live:** `Match.zone_definitions` (per match, saved, undo
  state) and a system library (§3).

## 2. Zone and tile tags

- **Zones:** a `tags` field on the zone and on its definition; a zone's
  tags are its definition's plus its own.
- **Tiles:** a `tags` field on tile templates (a template field, not data,
  so it is read live) plus a `tags` key in a tile's own data; a tile's tags
  are its template's plus its own.
- **Reads** (formulas): `zone_tags(zone)`, `zone_has_tag(zone, tag)`,
  `zones_with_tag(tag, map=)`, `zone_def_of(zone)`, `tile_tags(x, y)`,
  `tile_has_tag(x, y, tag)`, `tiles_with_tag(tag)` (the cells whose tile
  carries a tag; the tile reads take `map=` like the other unit-less spatial
  functions); `tag=` and `definition=` filters on `zones_at`,
  `entity_zones` and `zone_names` (`def` is a Python keyword, so formulas
  spell it `definition=`), and `tag=` / `def=` on `!zone list`.
- **Commands:** `!zone tags <zone> <tag ...>`, `!tile tags <x> <y> <tag
  ...>` (own tags), `!zone def tags`, `!tile def tags`, `!system tile
  tags`.
- Action tags are a separate idea (#234), later.

## 3. Commands and formulas (PR 1)

- `!zone def new | data | del-data | hook | del-hook | glyph | color |
  sprite | legend | tags | del [confirm] | list | info | copy | rename`,
  the grammar of `!tile def` (plus the dedicated look fields zones have).
- `!zone new <name> def=<d>` copies the definition's data into the new
  zone; `!zone new def=<d>` without a name mints one (below).
- `!zone setdef <zone> <d|->` attaches a definition (copying in the data
  keys the zone lacks) or detaches it.
- `!zone tags <zone> <tag ...>` (own tags).
- `!zone info` marks each value as the zone's own or inherited.
- **Deleting a definition in use** needs `confirm` and detaches the zones
  (they keep their own data and settings and stop naming it, so the
  definition's hooks, look and tags stop applying and a later definition
  of the same name takes none of them over; `!undo command` restores the
  links). Changed after the build (user call, audit Z1); `!tile def del`
  detaches placed tiles the same way.
- **Rename** for zone definitions AND tile templates (tile templates have
  none today): updates every zone / tile naming it and the match's other
  references, the way `!status rename` does.
- **System library:** `!system zone def ...`, `!match sync zones`,
  `!system zone promote`, the pattern of tile templates and status
  definitions.
- **Minted names:** `zone_spawn(definition, cells, map=None, name=None)`
  (mutating) makes a zone from a definition and returns its name, minted
  like unit ids (`poison_cloud`, `poison_cloud2`, ...) when no name is
  given. Prefab stamping uses the same minting.
- **Removing a zone from a formula:** `delete_zone(name)` already does it
  (the discussion assumed a missing `zone_drop`; none was needed).
  Lifetimes stay GM-composed: a definition's time hook counts down a
  `duration` in the zone's data and calls `delete_zone(zone_name)`. No
  built-in `expires_in`.

## 4. Later (feature list)

- **Tile and zone facing:** a `facing` field, per-facing glyphs and
  sprites, facing reads in formulas. Once it exists, prefab rotation turns
  tile and zone facing too.
- **`reapply`:** push a definition's / template's data to the zones and
  tiles made from it.

## 5. Prefabs: what one holds

A prefab is a saved rectangle of one map, stamped into a match at an
offset.

- **Contents:** tile data (tile template references kept as references),
  zones, units and the corpses lying in the rectangle. Map vars, fog
  memory and reveals are left out. All four kinds by default;
  `only=units,tiles,zones,corpses` picks a subset at save time.
- **The rectangle's edge:**
  - a zone partly inside is clipped to the rectangle's cells;
  - a body (footprint, located parts, snake segments, riders) is saved
    whole or not at all: one sticking out is left out with a ⚠️ naming it;
  - an aura (anchored zone) is saved only when its anchor is;
  - a unit carrying a map (nested maps) is left out with a ⚠️ saying
    units carrying maps can't go into a prefab (unsupported).
- **Units** are stored the way entity templates store them
  (`capture_template`: no ids, partial references kept, var names
  recorded), embedded in the prefab: a later entity-template edit never
  reaches a saved prefab.

## 6. Prefabs: storage and commands

- **Storage:** `templates/prefabs/<name>.json` with its own `.trash`;
  prefabs have their own namespace (a prefab and an entity template may
  share a name). Same saver permissions, 24 h version trash, storage-limit
  cuts and journal entries as templates.
- **Commands** (`!prefab` root):
  - `save <name> <x1> <y1> <x2> <y2> [map=] [only=] [definitions=all]`
  - `stamp <name> <x> <y> [map=] [mode=] [rotate=] [flip=] [team=]
    [confirm]`
  - `show`, `list`, `import`, `rename`, `copy`, `delete`, `trash`,
    `restore`
- **Permissions:** save needs a host of the channel's match or an admin
  (as `!template save`); stamp is a host command (a player's is queued);
  edits need the saver or an admin; reads are open to everyone.
- **`!prefab show <name> [text|image]`** renders the prefab through a scratch match,
  plus a contents summary; text or image follows the channel match's
  render mode (`!map mode`).
- **`!map new <name> from=<prefab>`** makes a map the prefab's size and
  stamps it.

## 7. Prefabs: stamping

- **Placement:** the prefab's top-left cell (after rotation) lands on
  (x, y); a stamp with any part of the rectangle off the map is refused.
- **Mode** (`mode=overlay|replace|keep`, default from the
  `prefab_stamp_mode` rule, overlay):
  - **overlay:** prefab tile data replaces the destination's cell by cell;
    cells without prefab data keep theirs.
  - **keep:** existing tile data wins.
  - **overlay and keep:** stamped units must fit; a unit landing where
    another unit stands (anything the occupancy code forbids) refuses the
    whole stamp, naming the blockers.
  - **replace:** the rectangle is cleared first: tile data wiped, zones
    lose their cells inside it (a zone left empty is removed), corpses
    lying there go, and units whose body covers any cell of it are
    despawned whole (`on_entity_despawned`, no death, no corpse). The
    first run lists what would be deleted; running it again with
    `confirm` stamps. `stamp_prefab(..., mode="replace")` in a formula
    needs `confirm=True`.
  - Corpses already lying at the destination stay in overlay and keep.
  - Units already inside the rectangle that block nothing stay (overlay,
    keep).
- **Blocking tiles don't refuse stamped units** (spawns aren't gated by
  block conditions today). Known issue, listed in CLAUDE.md §8.
- **Zone names:** a clash mints a fresh name (§3 minting); the reply names
  each rename.
- **Ids and links:** a unit keeps its saved id while the match has it
  free; otherwise one is minted from it (trailing digits dropped: `orc3`
  stamps as `orc`, `orc2`, ...), and a part named after its root follows
  the root's new id.
  Engine links inside the prefab (parts, mounts, `__follows`, aura
  anchors) are remapped to the new ids. GM vars holding a unit id as text
  stay as written. A group whose members are stamped is recreated, joining
  an existing group of the same name.
- **`team=<t>`** sets every stamped unit's team.
- **Rotation and mirroring:** `rotate=90|180|270`, `flip=h|v` turn the
  whole rectangle: unit facings turn, footprints swap W and H, located
  parts move to their turned offsets, zone shapes and tiles move.
  Directional meaning inside GM data (`fires: "left"`) stays as written
  (facing-relative directions are composable in formulas).
- **Hooks:** stamped units fire `on_entity_spawned` as spawns do; no tile
  enter / stop hooks. A `prefab_stamped` event goes on the bus (prefab,
  cell, map, the new unit ids and zone names). Stamped units join the
  turn order like spawns, keeping their saved initiative.
- **Undo:** a stamp is one undo step; `!dry !prefab stamp` previews one.
- **Journal:** command journals don't carry the prefab library, so a
  stamp (and a formula reading the library) is journaled as the state it
  left.

## 8. Prefabs and game systems

- A prefab records the game system it was saved under. A stamp is allowed
  when the destination match's system is that system or inherits from it;
  any other system is refused, naming both, unless the prefab was saved
  with `definitions=all`.
- The check is by name and inheritance only: a system edited since the
  save is expected (the prefab inherits the newer data; that is the point
  of templates over hardcoded stats).
- **Definitions stored with the prefab:** the match-level ones its
  contents use that the system lacks: tile templates, status definitions,
  zone definitions, and the match's `!func`s that stored formulas call.
  Tables, macros and aliases stay out. `definitions=all` stores every
  definition the contents use, system-level ones included (for
  cross-system copying; the exception).
- On a stamp a missing definition is added to the match; one that exists
  with different content keeps the destination's version, with a ⚠️.

## 9. Prefab formulas

- `stamp_prefab(name, x, y, map=, mode=, rotate=, flip=, team=,
  confirm=False)` (mutating) returns the new unit ids.
- `prefab_names()`, `prefab_size(name)` (reads).
