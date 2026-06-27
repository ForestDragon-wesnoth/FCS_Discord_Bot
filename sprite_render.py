## sprite_render.py (surface-agnostic graphics rendering)
#
# Turns the engine's pixel-agnostic render_scene() model into a Pillow image.
# Shared by every graphics surface (gui.py desktop window, the Discord image
# attachment) so the rendering lives in ONE place. Pure Pillow — no tkinter,
# no discord — so it loads and is fully unit-testable on a headless box.
from __future__ import annotations
import os
import io
from typing import Optional, Dict, Any, Tuple

try:
    from PIL import Image, ImageDraw, ImageOps, ImageColor, ImageFont
    _PIL_OK = True
except ImportError:  # Pillow is required for any graphics surface.
    Image = None  # type: ignore
    _PIL_OK = False

SPRITES_DIR_DEFAULT = "sprites"
ALLOWED_EXT = (".png",)
_BG_FILL = (20, 20, 24, 255)  # canvas backdrop behind everything
# Default procedural ground (a muted checkerboard) drawn when no background
# sprite is set, so empty cells read as terrain rather than a black void.
_GROUND_LIGHT = "#2c3230"
_GROUND_DARK = "#242927"


# ----------------------------------------------------------------------------
# Sprite loading (PNG-only, secure, cached).
# ----------------------------------------------------------------------------
class SpriteLoader:
    """Loads + caches PNG sprites from a folder. Security: PNG files ONLY
    (extension AND magic checked), and a key can't escape the folder (no
    absolute paths, no `..` traversal). Returns RGBA Pillow Images; a
    missing / non-PNG / unsafe key caches and returns None (the renderer
    then falls back to the glyph-as-text the model carries)."""

    def __init__(self, folder: str = SPRITES_DIR_DEFAULT):
        self.folder = os.path.abspath(folder)
        self._cache: Dict[Optional[str], Optional["Image.Image"]] = {}

    def _safe_path(self, key: Any) -> Optional[str]:
        if not isinstance(key, str) or not key.strip():
            return None
        key = key.strip().replace("\\", "/")
        base, ext = os.path.splitext(key)
        if ext == "":
            key = key + ".png"
        elif ext.lower() not in ALLOWED_EXT:
            return None  # only PNG
        full = os.path.normpath(os.path.join(self.folder, key))
        # Must stay inside the sprites folder (blocks `..` and absolute keys).
        if full != self.folder and not full.startswith(self.folder + os.sep):
            return None
        return full

    def get(self, key: Any) -> Optional["Image.Image"]:
        ck = key if isinstance(key, str) else None
        if ck in self._cache:
            return self._cache[ck]
        img: Optional["Image.Image"] = None
        path = self._safe_path(key)
        if path and os.path.isfile(path):
            try:
                with Image.open(path) as im:
                    if (im.format or "").upper() == "PNG":
                        img = im.convert("RGBA")
            except Exception:
                img = None
        self._cache[ck] = img
        return img

    def clear(self) -> None:
        self._cache.clear()


# ----------------------------------------------------------------------------
# Scene rendering (render_scene model -> Pillow Image).
# ----------------------------------------------------------------------------
class SceneRenderer:
    """Draws a render_scene() model to a Pillow RGBA Image."""

    def __init__(self, loader: SpriteLoader, cell_size: int = 100):
        self.loader = loader
        self.cell = max(1, int(cell_size))
        self._font = None

    def font(self):
        if self._font is None:
            size = max(8, int(self.cell * 0.6))
            try:
                self._font = ImageFont.load_default(size=size)
            except TypeError:  # older Pillow: load_default() takes no size
                self._font = ImageFont.load_default()
        return self._font

    # -- colour / alpha helpers ------------------------------------------
    @staticmethod
    def _rgb(name: Any) -> Optional[Tuple[int, int, int]]:
        if not isinstance(name, str) or not name.strip():
            return None
        try:
            return ImageColor.getrgb(name.strip())
        except (ValueError, Exception):
            return None

    @staticmethod
    def _scale_alpha(img: "Image.Image", opacity: int) -> "Image.Image":
        if opacity >= 100:
            return img
        op = max(0, min(100, int(opacity))) / 100.0
        a = img.getchannel("A").point(lambda v: int(v * op))
        out = img.copy()
        out.putalpha(a)
        return out

    def _tint(self, img: "Image.Image", tint: Any) -> "Image.Image":
        """Apply a tint: 'gray'/'grey' desaturates (a corpse); any other
        colour MULTIPLIES the sprite by it (a team tint). Alpha preserved."""
        if not isinstance(tint, str) or not tint.strip():
            return img
        t = tint.strip().lower()
        alpha = img.getchannel("A")
        if t in ("gray", "grey"):
            g = ImageOps.grayscale(img.convert("RGB")).convert("RGB")
            out = g.convert("RGBA")
            out.putalpha(alpha)
            return out
        rgb = self._rgb(t)
        if rgb is None:
            return img
        from PIL import ImageChops
        tint_layer = Image.new("RGB", img.size, rgb)
        mult = ImageChops.multiply(img.convert("RGB"), tint_layer).convert("RGBA")
        mult.putalpha(alpha)
        return mult

    # -- main entry ------------------------------------------------------
    def render(self, scene: Dict[str, Any]) -> "Image.Image":
        cell = self.cell
        vp = scene.get("viewport")
        if isinstance(vp, dict):
            ox, oy, cols, rows = vp["x"], vp["y"], vp["w"], vp["h"]
        else:
            ox, oy = 1, 1
            cols, rows = int(scene.get("grid_width", 1)), int(scene.get("grid_height", 1))
        cols, rows = max(1, cols), max(1, rows)
        W, H = cols * cell, rows * cell
        canvas = Image.new("RGBA", (W, H), _BG_FILL)

        def px(gx: int, gy: int) -> Tuple[int, int]:
            return (gx - ox) * cell, (gy - oy) * cell

        def in_window(gx: int, gy: int) -> bool:
            return ox <= gx <= ox + cols - 1 and oy <= gy <= oy + rows - 1

        bg = scene.get("background")
        drew_bg = False
        if isinstance(bg, dict):
            drew_bg = self._draw_background(canvas, bg, W, H)
        if not drew_bg:
            # No (loadable) background sprite: paint a primitive default ground
            # so empty cells read as terrain instead of a black void. A real
            # `!map background <sprite>` always wins.
            self._draw_default_ground(canvas, ox, oy, cols, rows)

        for p in sorted(scene.get("placements", []),
                        key=lambda d: d.get("layer", 0)):
            self._draw_placement(canvas, p, ox, oy, cols, rows)

        for f in scene.get("fog", []):
            if in_window(f.get("x"), f.get("y")):
                self._draw_fog(canvas, f, *px(f["x"], f["y"]))

        borders = scene.get("borders") or {}
        if borders.get("show"):
            self._draw_borders(canvas, borders, ox, oy, cols, rows)

        return canvas

    # -- layers ----------------------------------------------------------
    def _draw_background(self, canvas, bg, W, H) -> bool:
        img = self.loader.get(bg.get("sprite"))
        if img is None:
            return False
        mode = bg.get("mode", "stretch")
        if mode == "stretch":
            canvas.alpha_composite(img.resize((W, H)))
        elif mode == "center":
            x = (W - img.width) // 2
            y = (H - img.height) // 2
            canvas.alpha_composite(img, (x, y))
        else:  # tile
            iw, ih = max(1, img.width), max(1, img.height)
            for yy in range(0, H, ih):
                for xx in range(0, W, iw):
                    canvas.alpha_composite(img, (xx, yy))
        return True

    def _draw_default_ground(self, canvas, ox, oy, cols, rows):
        """A simple two-tone checkerboard so the grid of cells is visible when
        the GM hasn't set a background sprite. Drawn in GRID coordinates so the
        pattern stays stable as the viewport pans."""
        cell = self.cell
        light = ImageColor.getrgb(_GROUND_LIGHT) + (255,)
        dark = ImageColor.getrgb(_GROUND_DARK) + (255,)
        light_tile = Image.new("RGBA", (cell, cell), light)
        dark_tile = Image.new("RGBA", (cell, cell), dark)
        for r in range(rows):
            for c in range(cols):
                gx, gy = ox + c, oy + r
                tile = light_tile if (gx + gy) % 2 == 0 else dark_tile
                canvas.alpha_composite(tile, (c * cell, r * cell))

    def _draw_placement(self, canvas, p, ox, oy, cols, rows):
        cell = self.cell
        gx, gy = int(p.get("x", 1)), int(p.get("y", 1))
        w, h = max(1, int(p.get("w", 1))), max(1, int(p.get("h", 1)))
        if not (ox <= gx <= ox + cols - 1 and oy <= gy <= oy + rows - 1):
            return
        x0, y0 = (gx - ox) * cell, (gy - oy) * cell
        key = p.get("sprite")
        img = self.loader.get(key) if key else None
        opacity = int(p.get("opacity", 100))
        if img is not None:
            if p.get("flip_h"):
                img = ImageOps.mirror(img)
            if p.get("flip_v"):
                img = ImageOps.flip(img)
            img = self._tint(img, p.get("tint"))
            img = self._scale_alpha(img, opacity)
            mode = p.get("mode", "single")
            if mode == "stretch":
                canvas.alpha_composite(img.resize((w * cell, h * cell)), (x0, y0))
            elif mode == "tile":
                tiled = img.resize((cell, cell))
                for dy in range(h):
                    for dx in range(w):
                        canvas.alpha_composite(tiled, (x0 + dx * cell, y0 + dy * cell))
            else:  # single
                canvas.alpha_composite(img.resize((cell, cell)), (x0, y0))
        else:
            glyph = p.get("glyph")
            if isinstance(glyph, str) and glyph:
                self._draw_glyph(canvas, glyph, x0, y0, p.get("tint"), opacity)

    def _draw_glyph(self, canvas, glyph, x0, y0, tint, opacity):
        cell = self.cell
        rgb = self._rgb(tint) or (220, 220, 220)
        a = max(0, min(255, int(opacity / 100.0 * 255)))
        layer = Image.new("RGBA", (cell, cell), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        try:
            bbox = d.textbbox((0, 0), glyph, font=self.font())
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            tx = (cell - tw) // 2 - bbox[0]
            ty = (cell - th) // 2 - bbox[1]
        except Exception:
            tx = ty = cell // 4
        d.text((tx, ty), glyph, font=self.font(), fill=(rgb[0], rgb[1], rgb[2], a))
        canvas.alpha_composite(layer, (x0, y0))

    def _draw_fog(self, canvas, f, x0, y0):
        cell = self.cell
        opacity = int(f.get("opacity", 60))
        sprite = f.get("sprite")
        img = self.loader.get(sprite) if sprite else None
        if img is not None:
            canvas.alpha_composite(self._scale_alpha(img.resize((cell, cell)), opacity), (x0, y0))
        else:
            a = max(0, min(255, int(opacity / 100.0 * 255)))
            overlay = Image.new("RGBA", (cell, cell), (10, 10, 14, a))
            canvas.alpha_composite(overlay, (x0, y0))

    def _draw_borders(self, canvas, borders, ox, oy, cols, rows):
        cell = self.cell
        base_color = borders.get("color", "white")
        base_op = int(borders.get("opacity", 100))
        overrides = borders.get("overrides") or {}
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)

        def line_rgba(color, op):
            rgb = self._rgb(color) or (255, 255, 255)
            a = max(0, min(255, int(max(0, min(100, op)) / 100.0 * 255)))
            return (rgb[0], rgb[1], rgb[2], a)

        for gy in range(oy, oy + rows):
            for gx in range(ox, ox + cols):
                ov = overrides.get(f"{gx},{gy}") or {}
                color = ov.get("color", base_color)
                op = ov.get("opacity", base_op)
                x0, y0 = (gx - ox) * cell, (gy - oy) * cell
                d.rectangle([x0, y0, x0 + cell - 1, y0 + cell - 1],
                            outline=line_rgba(color, op))
        canvas.alpha_composite(overlay)


# ----------------------------------------------------------------------------
# Convenience: render a match straight to PNG bytes (for the Discord surface).
# ----------------------------------------------------------------------------
def render_match_png(match, loader: "SpriteLoader",
                     pov_team: Optional[str] = None,
                     viewport: Optional[Tuple[int, int, int, int]] = None,
                     cell_size: Optional[int] = None,
                     max_dim: int = 1600) -> bytes:
    """Render `match`'s graphics scene to PNG bytes. cell_size defaults to the
    sprite_cell_size rule; the result is downscaled to fit `max_dim` on its
    longest side (0 = no cap) so a big board stays a reasonable attachment.
    Raises RuntimeError if Pillow is unavailable."""
    if not _PIL_OK:
        raise RuntimeError("graphics rendering needs Pillow (pip install Pillow).")
    if cell_size is None:
        try:
            cell_size = int(match.rules.get("sprite_cell_size", 100))
        except (TypeError, ValueError):
            cell_size = 100
    scene = match.render_scene(pov_team=pov_team, viewport=viewport)
    img = SceneRenderer(loader, cell_size).render(scene)
    if max_dim and max(img.size) > max_dim:
        scale = max_dim / float(max(img.size))
        img = img.resize((max(1, int(img.width * scale)),
                          max(1, int(img.height * scale))))
    buf = io.BytesIO()
    img.convert("RGBA").save(buf, format="PNG")
    return buf.getvalue()
