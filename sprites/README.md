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

## Sprites are SERVER-SIDE only (intended, for now)

Sprite assets are managed on the **bot host's filesystem** — the operator drops
PNGs into this folder. The bot does **not** accept sprite uploads over Discord
(or any chat surface): there is no inbound attachment handling at all. Discord
attachments are **outbound only** (the rendered map PNG from `!map image` /
image auto-update boards); the `SpriteLoader` is read-only and the engine never
writes user-supplied files to disk.

This is a deliberate design choice while the bot is meant to be self-hosted
(e.g. on a laptop): validating the safety and legitimacy of arbitrary
user-uploaded image files on a publicly reachable bot is out of scope for now.
Keeping art server-side means each instance curates its own `sprites/` folder,
and there is no untrusted-file ingestion surface. An in-chat upload flow (with
proper validation/quotas) could be added later, but it is intentionally not
built today — do not add one without revisiting this decision.
