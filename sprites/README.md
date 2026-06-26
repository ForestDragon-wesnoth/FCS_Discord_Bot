# sprites/

PNG image assets for the graphics surface (`gui.py`).

- A sprite is referenced by a **key** stored in the match data — exactly like
  glyphs. Set them with the normal commands:
  - entity: `!ent set_var <id> sprite <key>` or `!ent set_var <id> sprites.<facing> <key>`
  - tile:   `!tile set <x> <y> sprite <key>` (or on a template via `!tile def data <tpl> sprite <key>`)
  - zone:   `!zone sprite <name> <key>`
  - background: `!map background <key> [stretch|tile|center]`
- The **key** is the filename relative to this folder, with or without `.png`
  (e.g. `hero` → `sprites/hero.png`; `mobs/orc` → `sprites/mobs/orc.png`).
- **PNG only.** Other formats are ignored (and the loader blocks `..` traversal
  and absolute paths for safety).
- A key that resolves to no file falls back to the entity/tile/zone **glyph**
  rendered as text — so sprites are fully optional and incremental.
- Cell size is the `sprite_cell_size` gamerule (default 100×100 px); a sprite is
  scaled to its footprint. Facing, mirror, multi-tile stretch/tile, tint,
  opacity, fog, and borders are all driven by the gamerules / data the engine's
  `render_scene()` model carries.

Run the surface with: `python gui.py [sprites_dir]` (needs `tkinter` + `Pillow`
and a display; use `cli.py` on a headless box).
