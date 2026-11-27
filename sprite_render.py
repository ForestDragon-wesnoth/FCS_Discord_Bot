## sprite_render.py (surface-agnostic graphics rendering)
#
# Turns the engine's pixel-agnostic render_scene() model into a Pillow image.
# Shared by every graphics surface (gui.py desktop window, the Discord image
# attachment) so the rendering lives in ONE place. Pure Pillow — no tkinter,
# no discord — so it loads and is fully unit-testable on a headless box.
from __future__ import annotations
import os
import io
from typing import Optional, Dict, Any, Tuple, List

try:
    from PIL import Image, ImageDraw, ImageOps, ImageColor, ImageFont
    _PIL_OK = True
except ImportError:  # Pillow is required for any graphics surface.
    Image = None  # type: ignore
    _PIL_OK = False

SPRITES_DIR_DEFAULT = "sprites"
ALLOWED_EXT = (".png",)
_BG_FILL = (20, 20, 24, 255)  # canvas backdrop behind everything
# Fallback ground colour: a flat fill painted when no background sprite is set
# OR the configured one (default `ground_default`) can't be loaded, so cells
# stay visible as terrain rather than a black void. A real background wins.
_GROUND_FALLBACK = "#5b4632"  # muted brown earth
# Fonts tried, in order, for a glyph outside ASCII (Pillow looks a bare file
# name up in the system font folders). A host can also drop a font into a
# `fonts/` folder of the sprites folder; those are tried first. Colour emoji
# fonts aren't listed: Pillow draws them only at fixed sizes.
_SYSTEM_FONTS = (
    "DejaVuSans.ttf", "seguisym.ttf", "segoeui.ttf", "arial.ttf",
    "Arial Unicode.ttf", "NotoSans-Regular.ttf", "NotoSansSymbols2-Regular.ttf",
    "FreeSans.ttf", "FreeSerif.ttf", "unifont.otf", "msgothic.ttc",
    "YuGothR.ttc", "msyh.ttc", "wqy-zenhei.ttc", "NotoSansCJK-Regular.ttc",
    "Apple Symbols.ttf", "AppleGothic.ttf",
)
_FONT_OK: Dict[Tuple[str, str], bool] = {}
# Loaded fonts by (source, size); None = the file can't be loaded. Shared by
# every renderer (gui.py builds one per redraw), since Pillow looks a bare
# font name up by walking the system font folders.
_FONT_CACHE: Dict[Tuple[Optional[str], int], Any] = {}


def _truetype(src: str, size: int):
    key = (src, size)
    if key not in _FONT_CACHE:
        try:
            _FONT_CACHE[key] = ImageFont.truetype(src, size)
        except (OSError, ValueError):
            _FONT_CACHE[key] = None
    return _FONT_CACHE[key]


def _load_font(src: Optional[str], size: int):
    if src is not None:
        f = _truetype(src, size)
        if f is not None:
            return f
    key = (None, size)
    if key not in _FONT_CACHE:
        try:
            _FONT_CACHE[key] = ImageFont.load_default(size=size)
        except TypeError:  # older Pillow: load_default() takes no size
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


def _font_has(src: str, text: str) -> bool:
    """Whether the font file `src` exists and draws every character of
    `text` as something other than its missing-character box."""
    key = (src, text)
    if key not in _FONT_OK:
        f = _truetype(src, 32)
        if f is None:
            _FONT_OK[key] = False
            return False
        try:

            def mask(c: str) -> bytes:
                im = Image.new("L", (48, 48))
                ImageDraw.Draw(im).text((4, 4), c, font=f, fill=255)
                return im.tobytes()
            missing = mask("\U000F0000")
            _FONT_OK[key] = all(
                (m := mask(c)) != missing and any(m)
                for c in text if not c.isspace())
        except (OSError, ValueError):
            _FONT_OK[key] = False
    return _FONT_OK[key]


# The colour palette (logic.TEXT_COLORS) names Pillow doesn't know: the
# bright_* variants, as the usual bright ANSI colours. The other palette
# names (red, gray, ...) are Pillow colour names already.
_PALETTE_RGB: Dict[str, Tuple[int, int, int]] = {
    "bright_red": (255, 85, 85), "bright_green": (85, 255, 85),
    "bright_yellow": (255, 255, 85), "bright_blue": (85, 85, 255),
    "bright_magenta": (255, 85, 255), "bright_cyan": (85, 255, 255),
    "bright_white": (255, 255, 255),
}
# The largest side a loaded sprite keeps (a bigger PNG is scaled down once).
_SPRITE_MAX_SIDE = 2048
# Share of a stretch-mode body (each side) its glyph fallback fills.
_GLYPH_FILL = 0.7
# The largest size a stretch body's glyph fallback is drawn at; a larger body
# scales the drawn shape up (only the part in the window is built).
_GLYPH_DRAW_MAX = 2048
# The smallest text of a name sprite, as a share of its short side: a word
# too long at this size is cut with "...".
_NAME_TEXT_MIN = 0.10
# Name sprites made, by (text, colours, size); shared by every renderer.
# Effects drawn around a finished body (#624), the rest change the picture.
_EDGE_EFFECTS = ("outline", "glow")
_NAME_TEXT_CACHE: Dict[Tuple[Any, ...], "Image.Image"] = {}


# ----------------------------------------------------------------------------
# Sprite loading (PNG-only, secure, cached).
# ----------------------------------------------------------------------------
class SpriteLoader:
    """Loads + caches PNG sprites from a folder. Security: PNG files ONLY
    (extension AND magic checked), and a key can't escape the folder (no
    absolute paths, no `..` traversal). Returns RGBA Pillow Images; a
    missing / non-PNG / unsafe key caches and returns None (the renderer
    then falls back to the glyph-as-text the model carries)."""

    def __init__(self, folder: str = SPRITES_DIR_DEFAULT,
                 first: Optional[str] = None):
        """`folder` is the shared sprites folder; `first`, when given, is a
        server's own sprites folder, searched before it (a server's PNG of
        the same key wins)."""
        self.folder = os.path.abspath(folder)
        self.folders = ([os.path.abspath(first)] if first else []) + [self.folder]
        self._cache: Dict[Optional[str], Tuple[Optional[str], Optional[float],
                                               Optional["Image.Image"]]] = {}

    def _safe_path(self, key: Any, folder: Optional[str] = None) -> Optional[str]:
        folder = folder or self.folder
        if not isinstance(key, str) or not key.strip():
            return None
        key = key.strip().replace("\\", "/")
        base, ext = os.path.splitext(key)
        if ext == "":
            key = key + ".png"
        elif ext.lower() not in ALLOWED_EXT:
            return None  # only PNG
        full = os.path.normpath(os.path.join(folder, key))
        # Must stay inside the sprites folder (blocks `..` and absolute keys).
        if full != folder and not full.startswith(folder + os.sep):
            return None
        return full

    def get(self, key: Any) -> Optional["Image.Image"]:
        """The sprite of `key`, re-read when the file it comes from changes
        (a PNG dropped in or replaced while the bot runs shows on the next
        render; the cache keeps the file's path and modification time)."""
        ck = key if isinstance(key, str) else None
        path, mtime = None, None
        for folder in self.folders:
            cand = self._safe_path(key, folder)
            if cand and os.path.isfile(cand):
                path = cand
                try:
                    mtime = os.path.getmtime(cand)
                except OSError:
                    mtime = None
                break
        hit = self._cache.get(ck)
        if hit is not None and hit[0] == path and hit[1] == mtime:
            return hit[2]
        img: Optional["Image.Image"] = None
        if path:
            try:
                with Image.open(path) as im:
                    if (im.format or "").upper() == "PNG":
                        img = im.convert("RGBA")
                        # Cells are at most a few hundred pixels: a huge
                        # PNG is kept at a size a render can use.
                        if max(img.size) > _SPRITE_MAX_SIDE:
                            img.thumbnail((_SPRITE_MAX_SIDE, _SPRITE_MAX_SIDE))
            except Exception:
                img = None
        self._cache[ck] = (path, mtime, img)
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
        self._glyph_fonts: Dict[str, Optional[str]] = {}
        self._tint_fill = 40  # set from the scene by render()
        self._base = self.cell

    def font(self, box: Optional[int] = None, glyph: str = ""):
        """The glyph font for a square of `box` pixels (default one cell).
        Pillow's built-in font has ASCII only, so a glyph outside it is
        drawn with the first font that has it (`_font_source_for`)."""
        size = max(8, int((box or self.cell) * 0.6))
        src = self._font_source_for(glyph) if glyph else None
        return _load_font(src, size)

    def _font_source_for(self, glyph: str) -> Optional[str]:
        """None (the built-in font) for ASCII; else the first font file —
        a `fonts/` folder of the sprites folders, then common system fonts
        — that has every character of `glyph`. None if none does (the
        built-in font then draws its empty-box glyph)."""
        if all(32 <= ord(c) < 127 for c in glyph):
            return None
        if glyph not in self._glyph_fonts:
            found = None
            for src in self._font_candidates():
                if _font_has(src, glyph):
                    found = src
                    break
            self._glyph_fonts[glyph] = found
        return self._glyph_fonts[glyph]

    def _font_candidates(self) -> List[str]:
        out: List[str] = []
        for folder in getattr(self.loader, "folders", [self.loader.folder]):
            fdir = os.path.join(folder, "fonts")
            if os.path.isdir(fdir):
                out.extend(os.path.join(fdir, n) for n in sorted(os.listdir(fdir))
                           if n.lower().endswith((".ttf", ".otf", ".ttc")))
        return out + list(_SYSTEM_FONTS)

    # -- colour / alpha helpers ------------------------------------------
    @staticmethod
    def _rgb(name: Any) -> Optional[Tuple[int, int, int]]:
        # W27: an [r, g, b] list and `r,g,b` text too (Pillow reads names
        # and hex).
        if isinstance(name, (list, tuple)) and len(name) == 3:
            try:
                return tuple(max(0, min(255, int(c))) for c in name)
            except (TypeError, ValueError):
                return None
        if not isinstance(name, str) or not name.strip():
            return None
        key = name.strip().lower()
        if key in _PALETTE_RGB:
            return _PALETTE_RGB[key]
        parts = key.split(",")
        if len(parts) == 3 and all(p.strip().isdigit() for p in parts):
            return tuple(min(255, int(p)) for p in parts)
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

    def _tint(self, img: "Image.Image", tint: Any,
              strength: int = 100) -> "Image.Image":
        """Apply a tint: 'gray'/'grey' desaturates (a corpse); any other
        colour MULTIPLIES the sprite by it. `strength` (0-100) blends the
        result with the original. Alpha preserved."""
        if not isinstance(tint, str) or not tint.strip() or strength <= 0:
            return img
        if strength < 100:
            full = self._tint(img, tint)
            return Image.blend(img, full, strength / 100.0)
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
    def _render_blank(self, text: str) -> "Image.Image":
        """A blank board (a map the viewing team hasn't seen): its text on
        the empty background, the same text the ASCII board shows."""
        from PIL import ImageDraw
        img = Image.new("RGBA", _blank_size(text), _BG_FILL)
        draw = ImageDraw.Draw(img)
        font = _load_font(self._font_source_for(text), 16)
        draw.text((8, 14), text, fill=(220, 220, 220, 255), font=font)
        return img

    def render(self, scene: Dict[str, Any]) -> "Image.Image":
        if scene.get("blank"):
            return self._render_blank(str(scene["blank"]))
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
        try:
            self._tint_fill = max(0, min(100, int(scene.get("tint_fill_opacity", 40))))
            self._outline_pct = max(1, min(50, int(scene.get(
                "zone_outline_width", 12))))
        except (TypeError, ValueError):
            self._tint_fill = 40
            self._outline_pct = 12
        try:
            self._base = max(1, int(scene.get("sprite_cell_size", cell)))
        except (TypeError, ValueError):
            self._base = cell

        def px(gx: int, gy: int) -> Tuple[int, int]:
            return (gx - ox) * cell, (gy - oy) * cell

        def in_window(gx: int, gy: int) -> bool:
            return ox <= gx <= ox + cols - 1 and oy <= gy <= oy + rows - 1

        bg = scene.get("background")
        drew_bg = False
        if isinstance(bg, dict):
            drew_bg = self._draw_background(canvas, bg, scene, ox, oy, cols, rows)
        if not drew_bg:
            # No (loadable) background sprite: paint a primitive default ground
            # so empty cells read as terrain instead of a black void. A real
            # `!map background <sprite>` always wins.
            self._draw_default_ground(canvas, ox, oy, cols, rows)

        # Void cells (no cell there): the ground taken back out, then the
        # void sprite when one is set; no border is drawn round them.
        voids = {(int(c[0]), int(c[1])) for c in scene.get("voids") or []
                 if in_window(int(c[0]), int(c[1]))}
        if voids:
            blank = Image.new("RGBA", (cell, cell), _BG_FILL)
            vimg = self.loader.get(scene.get("void_sprite")) \
                if scene.get("void_sprite") else None
            piece = vimg.resize((cell, cell)) if vimg is not None else None
            for gx, gy in voids:
                canvas.paste(blank, px(gx, gy))
                if piece is not None:
                    canvas.alpha_composite(piece, px(gx, gy))

        # Grid border lines: drawn ABOVE the ground/background but BELOW tiles,
        # zones, and entities (so they aid alignment without occluding content).
        borders = scene.get("borders") or {}
        if borders.get("show"):
            self._draw_borders(canvas, borders, ox, oy, cols, rows, voids)

        for p in sorted(scene.get("placements", []),
                        key=lambda d: d.get("layer", 0)):
            self._draw_placement(canvas, p, ox, oy, cols, rows)

        for f in scene.get("fog", []):
            if in_window(f.get("x"), f.get("y")):
                self._draw_fog(canvas, f, *px(f["x"], f["y"]))

        # Highlights (`!map preview`): translucent squares over the covered
        # cells, drawn last so they sit ABOVE units (and fog).
        for hl in scene.get("highlights") or []:
            self._draw_highlight(canvas, hl, ox, oy, cols, rows)

        if scene.get("coords"):
            canvas = self._add_rulers(canvas, ox, oy, cols, rows)
        entries = list(scene.get("legend") or [])
        if scene.get("legend") is not None and scene.get("highlights"):
            for hl in scene["highlights"]:
                entries.append({"kind": "highlight", "rgb": hl.get("rgb"),
                                "opacity": hl.get("opacity"),
                                "labels": ["preview area"]})
        if entries:
            canvas = self._add_legend(canvas, entries)
        return canvas

    # The image legend's sizes, in pixels whatever the cell size, so it stays
    # readable on a zoomed-out map.
    _LEGEND_SWATCH = 28
    _LEGEND_TEXT = 15
    _LEGEND_MAX = 40
    _LEGEND_LABEL_MAX = 300     # characters; no image is wide enough for more

    def _legend_swatch(self, entry) -> "Image.Image":
        """A small square showing an entry's look, drawn as on the map."""
        S = self._LEGEND_SWATCH
        sw = Image.new("RGBA", (S, S), ImageColor.getrgb(_GROUND_FALLBACK) + (255,))
        kind = entry.get("kind")
        if kind == "highlight":
            try:
                r, g, b = (max(0, min(255, int(c))) for c in entry.get("rgb") or (255, 64, 64))
            except (TypeError, ValueError):
                r, g, b = 255, 64, 64
            a = int(max(0, min(100, int(entry.get("opacity") or 40))) / 100.0 * 255)
            sw.alpha_composite(Image.new("RGBA", (S, S), (r, g, b, a)))
            return sw
        mini = SceneRenderer(self.loader, S)
        mini._tint_fill, mini._base = self._tint_fill, self._base
        if kind == "fog":
            mini._draw_fog(sw, entry, 0, 0)
            return sw
        p = {k: v for k, v in entry.items() if k != "labels"}
        p.update({"x": 1, "y": 1, "w": 1, "h": 1, "mode": "single"})
        p.setdefault("opacity", 100)
        mini._draw_placement(sw, p, 1, 1, 1, 1)
        return sw

    def _add_legend(self, canvas, entries):
        """The map with a legend below it: a swatch per look and its
        meanings, in columns as wide as the map allows."""
        S, T = self._LEGEND_SWATCH, self._LEGEND_TEXT
        shown = entries[:self._LEGEND_MAX]
        more = len(entries) - len(shown)
        # No column is wider than the widest image, so a label longer than
        # that many characters can't show anyway: it is cut first.
        cap = self._LEGEND_LABEL_MAX
        texts = []
        for e in shown:
            t = ", ".join(map(str, e.get("labels") or []))
            texts.append(t if len(t) <= cap else t[:cap] + "…")
        if more:
            texts.append(f"…and {more} more")
        probe = ImageDraw.Draw(Image.new("L", (1, 1)))

        def width(t: str, f=None) -> int:
            f = f or self.font(int(T / 0.6), t)
            try:
                b = probe.textbbox((0, 0), t, font=f)
                return b[2] - b[0]
            except Exception:
                return len(t) * T // 2
        pad, gap, line = 8, 8, S + 6
        colw = min(max(canvas.width - 2 * pad, 120),
                   S + gap + max(width(t) for t in texts) + 2 * gap)
        room = colw - S - 2 * gap
        for i, t in enumerate(texts):
            if width(t) > room:
                # One font for the label and its "…" (resolving it per
                # length tried rendered every character again), and the
                # cut found by halving.
                f = self.font(int(T / 0.6), t + "…")
                lo, hi = 1, len(t)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if width(t[:mid] + "…", f) <= room:
                        lo = mid
                    else:
                        hi = mid - 1
                texts[i] = t[:lo] + "…"
        ncols = max(1, (canvas.width - 2 * pad) // colw)
        nrows = -(-len(texts) // ncols)
        # At least one column wide: on a map narrower than that, the legend
        # widens the image (the map stays at the left).
        out = Image.new("RGBA", (max(canvas.width, colw + 2 * pad),
                                 canvas.height + 2 * pad + nrows * line),
                        _BG_FILL)
        out.alpha_composite(canvas, (0, 0))
        d = ImageDraw.Draw(out)
        for i, t in enumerate(texts):
            c, r = i % ncols, i // ncols
            x, y = pad + c * colw, canvas.height + pad + r * line
            if i < len(shown):
                out.alpha_composite(self._legend_swatch(shown[i]), (x, y))
                tx = x + S + gap
            else:
                tx = x
            f = self.font(int(T / 0.6), t)
            d.text((tx, y + (S - T) // 2 - 1), t, font=f, fill=(220, 220, 220, 255))
        return out

    def _ruler_margin(self, labels: int) -> int:
        """Pixels of margin a ruler with numbers up to `labels` takes."""
        return ruler_margin(self.cell, labels)

    def _add_rulers(self, canvas, ox, oy, cols, rows):
        """The map with coordinate labels in a margin along the top (x) and
        the left (y), numbered as commands take them (1-based) and following
        the viewport window. Graphics have room for whole numbers, so no
        digit stacking as in the ASCII rulers."""
        cell = self.cell
        top = self._ruler_margin(1)
        left = self._ruler_margin(oy + rows - 1)
        out = Image.new("RGBA", (canvas.width + left, canvas.height + top),
                        _BG_FILL)
        out.alpha_composite(canvas, (left, top))
        d = ImageDraw.Draw(out)
        font = _label_font(cell)
        fill = (200, 200, 200, 255)

        def centred(text, cx, cy):
            try:
                b = d.textbbox((0, 0), text, font=font)
                w, h = b[2] - b[0], b[3] - b[1]
                d.text((cx - w // 2 - b[0], cy - h // 2 - b[1]), text,
                       font=font, fill=fill)
            except Exception:
                d.text((cx, cy), text, font=font, fill=fill)

        for i in range(cols):
            centred(str(ox + i), left + i * cell + cell // 2, top // 2)
        for j in range(rows):
            centred(str(oy + j), left // 2, top + j * cell + cell // 2)
        return out

    # -- layers ----------------------------------------------------------
    def _draw_background(self, canvas, bg, scene, ox, oy, cols, rows) -> bool:
        """The background sprite, placed on the WHOLE grid and cut to the
        window, so it stays put under the units when the view pans:
        `stretch` = one copy over the whole map; `tile` = one copy per cell;
        `center` = one copy at its own size (drawn for sprite_cell_size
        cells, scaled with the cell) at the middle of the map."""
        img = self.loader.get(bg.get("sprite"))
        if img is None:
            return False
        cell = self.cell
        W, H = cols * cell, rows * cell
        gw = max(1, int(scene.get("grid_width", cols)))
        gh = max(1, int(scene.get("grid_height", rows)))
        # The window's top-left in map pixels.
        wx, wy = (ox - 1) * cell, (oy - 1) * cell
        mode = bg.get("mode", "stretch")
        if mode == "stretch":
            # Map pixels -> source pixels; only the window's part is resized.
            sx, sy = img.width / (gw * cell), img.height / (gh * cell)
            box = (wx * sx, wy * sy, (wx + W) * sx, (wy + H) * sy)
            canvas.alpha_composite(img.resize((W, H), box=box))
        elif mode == "center":
            try:
                base = max(1, int(scene.get("sprite_cell_size", cell)))
            except (TypeError, ValueError):
                base = cell
            k = cell / base
            iw, ih = max(1, round(img.width * k)), max(1, round(img.height * k))
            x = (gw * cell - iw) // 2 - wx
            y = (gh * cell - ih) // 2 - wy
            if x + iw > 0 and y + ih > 0 and x < W and y < H:
                part = img.resize((iw, ih))
                sx0, sy0 = max(0, -x), max(0, -y)
                canvas.alpha_composite(part, (x + sx0, y + sy0), (sx0, sy0))
        else:  # tile: one copy per cell
            piece = img.resize((cell, cell))
            for yy in range(0, H, cell):
                for xx in range(0, W, cell):
                    canvas.alpha_composite(piece, (xx, yy))
        return True

    def _draw_default_ground(self, canvas, ox, oy, cols, rows):
        """A flat ground fill, painted when no background sprite is set or the
        configured one couldn't be loaded, so the map reads as terrain rather
        than a black void. A real background sprite always wins."""
        rgb = ImageColor.getrgb(_GROUND_FALLBACK) + (255,)
        canvas.alpha_composite(Image.new("RGBA", canvas.size, rgb))

    def _draw_placement(self, canvas, p, ox, oy, cols, rows):
        """One placement. Only the part of its body inside the window (plus
        a margin for the outline) is built, then pasted: a body whose anchor
        is off the window still shows its other cells, and a huge body costs
        what the window shows, never its whole area in pixels."""
        cell = self.cell
        gx, gy = int(p.get("x", 1)), int(p.get("y", 1))
        w, h = max(1, int(p.get("w", 1))), max(1, int(p.get("h", 1)))
        mode = p.get("mode", "single")
        if mode not in ("stretch", "tile"):
            w = h = 1  # single: one cell at the anchor
        if gx + w - 1 < ox or gx > ox + cols - 1 \
                or gy + h - 1 < oy or gy > oy + rows - 1:
            return
        key = p.get("sprite")
        img = self.loader.get(key) if key else None
        label = p.get("label")
        if img is None and isinstance(label, str) and label:
            # A name sprite (entity_sprite_fallback name_text): made here
            # and drawn like any sprite. A stretch body's is made at the
            # body's shape, so its text runs along the body.
            if mode == "stretch":
                bw, bh = w * cell, h * cell
                k = min(1.0, _GLYPH_DRAW_MAX / max(bw, bh))
                img = self.name_text_image(label, p.get("label_color"),
                                           p.get("label_bg"),
                                           max(1, round(bw * k)),
                                           max(1, round(bh * k)),
                                           fit=p.get("label_fit"))
            else:
                img = self.name_text_image(label, p.get("label_color"),
                                           p.get("label_bg"), cell, cell,
                                           fit=p.get("label_fit"))
        opacity = int(p.get("opacity", 100))
        glyph = p.get("glyph")
        has_glyph = isinstance(glyph, str) and bool(glyph)
        # Sprite effects (#624): `effects` in the order they apply; a glyph
        # drawn in its place skips the records meant for pictures only (the
        # team look: a glyph is drawn in the unit's colour already), and a
        # corpse's glyph takes `glyph_effects`.
        if img is not None:
            fx = list(p.get("effects") or [])
        else:
            fx = [r for r in (p.get("glyph_effects", p.get("effects")) or [])
                  if isinstance(r, dict) and not r.get("picture_only")]
        fx = [r for r in fx if isinstance(r, dict)]
        team = self._rgb(p.get("tint"))
        edges = [r for r in fx if r.get("effect") in _EDGE_EFFECTS]
        body_fx = [r for r in fx if r.get("effect") not in _EDGE_EFFECTS]
        ring_px = [self._edge_px(r) for r in edges]
        margin = (sum(ring_px) + 2) if edges else 0
        # The body's pixels, and the box of them that is drawn: the window
        # plus `margin` on each side (the outline reads its neighbours).
        W, H = w * cell, h * cell
        x0, y0 = (gx - ox) * cell, (gy - oy) * cell
        box = (max(0, -x0 - margin), max(0, -y0 - margin),
               min(W, cols * cell - x0 + margin),
               min(H, rows * cell - y0 + margin))
        if box[2] <= box[0] or box[3] <= box[1]:
            return
        if img is None and has_glyph and body_fx and mode == "stretch":
            # A stretch body's glyph with effects: drawn whole (capped at
            # _GLYPH_DRAW_MAX) and then handled as a picture, so a turn or a
            # flip acts on the whole body.
            k = min(1.0, _GLYPH_DRAW_MAX / max(W, H))
            bw, bh = max(1, round(W * k)), max(1, round(H * k))
            img = self._fill_glyph(glyph, bw, bh, p.get("glyph_tint", p.get(
                "tint")), int(p.get("glyph_opacity", opacity)))
            opacity = 100
        piece = None        # one cell's look, tiled over the box
        body = None         # the box's look, built directly
        if img is not None:
            if p.get("flip_h"):
                img = ImageOps.mirror(img)
            if p.get("flip_v"):
                img = ImageOps.flip(img)
            if p.get("kind") != "entity":
                img = self._tint(img, p.get("tint"))
            img = self._apply_effects(img, body_fx, team)
            img = self._scale_alpha(img, opacity)
            if mode == "stretch":
                iw, ih = img.size
                body = img.resize(
                    (box[2] - box[0], box[3] - box[1]),
                    box=(box[0] * iw / W, box[1] * ih / H,
                         box[2] * iw / W, box[3] * ih / H))
            else:
                piece = img.resize((cell, cell))
        else:
            if not has_glyph:
                # A coloured zone / tile with no sprite or glyph: a
                # translucent square of its colour (ASCII tints the `.`).
                # Only those: a corpse's or overlay's tint recolours its
                # sprite, so a missing PNG there draws nothing.
                if p.get("kind") not in ("zone", "tile"):
                    return
                if p.get("edges"):
                    # An outlined zone (#335): its edge, no fill.
                    self._draw_edges(canvas, p, ox, oy)
                    return
                rgb = self._rgb(p.get("tint"))
                fill = self._tint_fill * opacity // 100
                if rgb is None or fill <= 0:
                    return
                a = max(0, min(255, int(fill / 100.0 * 255)))
                piece = Image.new("RGBA", (cell, cell), tuple(rgb[:3]) + (a,))
                piece = self._apply_effects(piece, body_fx, team)
                mode = "tile"
            else:
                gtint = p.get("glyph_tint", p.get("tint"))
                gop = int(p.get("glyph_opacity", opacity))
                if mode == "stretch":
                    body = self._fill_glyph(glyph, W, H, gtint, gop, box)
                else:
                    piece = self._apply_effects(
                        self._glyph_layer(glyph, cell, gtint, gop),
                        body_fx, team)
        if body is None:
            body = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]),
                             (0, 0, 0, 0))
            for dy in range(box[1] // cell, (box[3] - 1) // cell + 1):
                for dx in range(box[0] // cell, (box[2] - 1) // cell + 1):
                    px, py = dx * cell - box[0], dy * cell - box[1]
                    body.alpha_composite(piece, (max(0, px), max(0, py)),
                                         (max(0, -px), max(0, -py)))
        if edges:
            body = self._edge_rings(body, edges, ring_px, team)
        bx, by = x0 + box[0], y0 + box[1]
        sx, sy = max(0, -bx), max(0, -by)
        canvas.alpha_composite(body, (bx + sx, by + sy), (sx, sy))
        if p.get("edges"):
            self._draw_edges(canvas, p, ox, oy)

    def _draw_edges(self, canvas, p, ox, oy) -> None:
        """An outlined zone cell's outside sides (#335, `edges`: some of
        "nesw") as lines in the zone's colour (white without one), each
        zone_outline_width percent of the cell thick, inside the cell."""
        cell = self.cell
        rgb = self._rgb(p.get("tint")) or (255, 255, 255)
        t = max(1, round(cell * self._outline_pct / 100.0))
        x0 = (int(p.get("x", 1)) - ox) * cell
        y0 = (int(p.get("y", 1)) - oy) * cell
        a = max(0, min(255, int(int(p.get("opacity", 100)) * 255 / 100)))
        col = tuple(rgb[:3]) + (a,)
        sides = str(p.get("edges") or "")
        from PIL import ImageDraw
        layer = Image.new("RGBA", (cell, cell), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        if "n" in sides:
            d.rectangle((0, 0, cell - 1, t - 1), fill=col)
        if "s" in sides:
            d.rectangle((0, cell - t, cell - 1, cell - 1), fill=col)
        if "w" in sides:
            d.rectangle((0, 0, t - 1, cell - 1), fill=col)
        if "e" in sides:
            d.rectangle((cell - t, 0, cell - 1, cell - 1), fill=col)
        sx, sy = max(0, -x0), max(0, -y0)
        if sx < cell and sy < cell:
            canvas.alpha_composite(layer, (x0 + sx, y0 + sy), (sx, sy))

    # -- sprite effects (#624) -------------------------------------------
    def _effect_rgb(self, rec: Dict[str, Any], team: Optional[Tuple[int, ...]]
                    ) -> Optional[Tuple[int, int, int]]:
        """A record's colour: an [r, g, b] list, a name or #rrggbb, `team`
        (the placement's colour) or `team_light` (that colour halfway to
        white); its `fallback` when that has nothing, else None (the
        record is skipped)."""
        for key in ("color", "fallback"):
            v = rec.get(key)
            rgb: Optional[Tuple[int, ...]] = None
            if isinstance(v, (list, tuple)) and len(v) == 3:
                try:
                    rgb = tuple(max(0, min(255, int(c))) for c in v)
                except (TypeError, ValueError):
                    rgb = None
            elif v == "team":
                rgb = team
            elif v == "team_light":
                rgb = (tuple((c + 255) // 2 for c in team[:3])
                       if team is not None else None)
            elif isinstance(v, str):
                rgb = self._rgb(v)
            if rgb is not None:
                return (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        return None

    @staticmethod
    def _num(rec: Dict[str, Any], key: str, default: float) -> float:
        v = rec.get(key, default)
        try:
            v = float(v)
        except (TypeError, ValueError):
            return default
        return v if v == v else default

    def _apply_effects(self, img: "Image.Image", fx: List[Dict[str, Any]],
                       team: Optional[Tuple[int, ...]]) -> "Image.Image":
        """`img` with each effect that changes the picture itself applied
        in order (the edge effects come later, around the built body).
        Alpha is kept, but for `opacity`, `rotate`, `scale` and `flip`."""
        if not fx:
            return img
        from PIL import ImageChops, ImageEnhance
        img = img.convert("RGBA")
        for rec in fx:
            eff = rec.get("effect")
            alpha = img.getchannel("A")
            rgb = img.convert("RGB")
            strength = max(0.0, min(100.0, self._num(rec, "strength", 100)))
            new = None
            if eff in ("tint", "blend"):
                col = self._effect_rgb(rec, team)
                if col is None:
                    continue
                layer = Image.new("RGB", img.size, col)
                mode = rec.get("mode", "multiply") if eff == "blend" \
                    else "multiply"
                op = {"multiply": ImageChops.multiply,
                      "screen": ImageChops.screen,
                      "overlay": ImageChops.overlay,
                      "add": ImageChops.add,
                      "subtract": ImageChops.subtract,
                      "difference": ImageChops.difference,
                      "lighten": ImageChops.lighter,
                      "darken": ImageChops.darker}.get(mode)
                if op is None:
                    continue
                new = op(rgb, layer)
            elif eff == "hue":
                hsv = rgb.convert("HSV")
                h, s_, v = hsv.split()
                shift = int(round(self._num(rec, "degrees", 0) / 360.0 * 256))
                h = h.point(lambda x: (x + shift) % 256)
                new = Image.merge("HSV", (h, s_, v)).convert("RGB")
            elif eff == "saturation":
                new = ImageEnhance.Color(rgb).enhance(
                    self._num(rec, "amount", 100) / 100.0)
            elif eff == "greyscale":
                new = ImageOps.grayscale(rgb).convert("RGB")
            elif eff == "brightness":
                new = ImageEnhance.Brightness(rgb).enhance(
                    self._num(rec, "amount", 100) / 100.0)
            elif eff == "contrast":
                new = ImageEnhance.Contrast(rgb).enhance(
                    self._num(rec, "amount", 100) / 100.0)
            elif eff == "invert":
                new = ImageOps.invert(rgb)
            elif eff == "opacity":
                img = self._scale_alpha(img, int(self._num(rec, "amount", 100)))
                continue
            elif eff == "flip":
                axis = rec.get("axis", "h")
                if axis in ("h", "both"):
                    img = ImageOps.mirror(img)
                if axis in ("v", "both"):
                    img = ImageOps.flip(img)
                continue
            elif eff == "rotate":
                deg = self._num(rec, "degrees", 0)
                if deg % 360:
                    img = img.rotate(-deg, resample=Image.BICUBIC,
                                     expand=False)
                continue
            elif eff == "scale":
                k = self._num(rec, "percent", 100) / 100.0
                if k > 0 and k != 1:
                    nw = max(1, round(img.width * k))
                    nh = max(1, round(img.height * k))
                    small = img.resize((nw, nh), Image.LANCZOS)
                    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
                    out.alpha_composite(
                        small.crop((max(0, (nw - img.width) // 2),
                                    max(0, (nh - img.height) // 2),
                                    max(0, (nw - img.width) // 2)
                                    + min(nw, img.width),
                                    max(0, (nh - img.height) // 2)
                                    + min(nh, img.height))),
                        (max(0, (img.width - nw) // 2),
                         max(0, (img.height - nh) // 2)))
                    img = out
                continue
            if new is None:
                continue
            if strength < 100:
                new = Image.blend(rgb, new, strength / 100.0)
            img = new.convert("RGBA")
            img.putalpha(alpha)
        return img

    def _edge_px(self, rec: Dict[str, Any]) -> int:
        """An outline's or glow's width at sprite_cell_size cells, scaled to
        this cell size, in pixels."""
        w = self._num(rec, "width", 3 if rec.get("effect") == "outline" else 8)
        return max(1, round(w * self.cell / max(1, self._base)))

    def _edge_rings(self, body, edges, widths, team):
        """`body` with its outline / glow rings, each outside the ones
        before it: out from the drawn shape where the body has transparent
        room, and in from the body's border where the shape reaches it (so
        the last ring is outermost there too). A glow is its ring
        blurred."""
        from PIL import ImageFilter, ImageChops
        alpha = body.getchannel("A").point(lambda v: 255 if v >= 128 else 0)
        total = sum(widths)
        pad = Image.new("L", (body.width + 2 * total, body.height + 2 * total),
                        0)
        pad.paste(alpha, (total, total))

        def grown(d: int):
            if d <= 0:
                return pad
            return pad.filter(ImageFilter.MaxFilter(2 * d + 1))

        def band(inset: int, wpx: int):
            # The shape's pixels from `inset` to inset + wpx in from the
            # body's own border.
            fr = Image.new("L", body.size, 0)
            d = ImageDraw.Draw(fr)
            o, i = inset, inset + wpx
            if body.width > 2 * o and body.height > 2 * o:
                d.rectangle([o, o, body.width - 1 - o, body.height - 1 - o],
                            fill=255)
                if body.width > 2 * i and body.height > 2 * i:
                    d.rectangle([i, i, body.width - 1 - i,
                                 body.height - 1 - i], fill=0)
            return ImageChops.multiply(alpha, fr)

        out = body.copy()
        before = 0
        for n, (rec, wpx) in enumerate(zip(edges, widths)):
            rgb = self._effect_rgb(rec, team)
            after = sum(widths[n + 1:])
            if rgb is None:
                before += wpx
                continue
            outer = ImageChops.subtract(grown(before + wpx), grown(before))
            outer = outer.crop((total, total, total + body.width,
                                total + body.height))
            op = max(0.0, min(100.0, self._num(rec, "opacity", 100))) / 100.0
            inner = band(after, wpx)
            if rec.get("effect") == "glow":
                # A soft halo: the outer ring blurred and drawn under the
                # picture, the band inside the border blurred over it.
                blur = ImageFilter.GaussianBlur(max(1, wpx / 2))
                halo = Image.new("RGBA", body.size, rgb + (0,))
                halo.putalpha(outer.filter(blur).point(
                    lambda v, k=op: int(v * k)))
                halo.alpha_composite(out)
                out = halo
                ring = inner.filter(blur)
            else:
                ring = ImageChops.lighter(outer, inner)
            ring = ring.point(lambda v, k=op: int(v * k))
            layer = Image.new("RGBA", body.size, rgb + (0,))
            layer.putalpha(ring)
            out.alpha_composite(layer)
            before += wpx
        return out

    def name_text_image(self, text: str, color: Any, bg: Any,
                        pw: int, ph: int, fit: Any = None) -> "Image.Image":
        """A pw x ph name sprite: `bg` filling it, the lines of `text` (one
        per line break) centred in `color` at the largest size at which
        every line fits. A line too wide even at the smallest size
        (_NAME_TEXT_MIN of the short side) is cut with "...", and lines
        that don't fit below each other are dropped, the last kept one
        ending with "...". With `fit` per_line (name_text_per_line_size)
        each line takes its own size: as large as its width allows, the
        lines whose width stops them keeping that size and the others
        sharing the height left over (`_per_line_sizes`)."""
        ck = (text, str(color), str(bg), pw, ph, fit)
        got = _NAME_TEXT_CACHE.get(ck)
        if got is not None:
            return got
        fg = self._rgb(color) or (255, 255, 255)
        back = self._rgb(bg) or (0, 0, 0)
        img = Image.new("RGBA", (pw, ph), tuple(back[:3]) + (255,))
        lines = [ln for ln in text.split("\n") if ln] or [text]
        src = self._font_source_for(text.replace("\n", ""))
        pad = max(1, round(min(pw, ph) * 0.06))
        aw, ah = max(1, pw - 2 * pad), max(1, ph - 2 * pad)
        d = ImageDraw.Draw(img)

        def width(t: str, font) -> int:
            b = d.textbbox((0, 0), t, font=font)
            return b[2] - b[0]

        def line_h(font) -> int:
            try:
                a, de = font.getmetrics()
                return max(1, a + de)
            except Exception:
                b = d.textbbox((0, 0), "Ag", font=font)
                return max(1, b[3] - b[1])

        def fits(size: int) -> bool:
            font = _load_font(src, size)
            return (line_h(font) * len(lines) <= ah
                    and all(width(ln, font) <= aw for ln in lines))
        lo = max(6, round(min(pw, ph) * _NAME_TEXT_MIN))
        hi = max(lo, ah)
        size = lo
        if fit == "per_line" and len(lines) > 1:
            sizes = self._per_line_sizes(lines, src, lo, hi, aw, ah,
                                         width, line_h)
            if sizes is not None:
                fonts = [_load_font(src, z) for z in sizes]
                hs = [line_h(f) for f in fonts]
                top = (ph - sum(hs)) // 2
                for ln, f, lh in zip(lines, fonts, hs):
                    b = d.textbbox((0, 0), ln, font=f)
                    x = (pw - (b[2] - b[0])) // 2 - b[0]
                    d.text((x, top), ln, font=f,
                           fill=tuple(fg[:3]) + (255,))
                    top += lh
                if len(_NAME_TEXT_CACHE) >= 512:
                    _NAME_TEXT_CACHE.clear()
                _NAME_TEXT_CACHE[ck] = img
                return img
        if fits(lo):
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if fits(mid):
                    lo = mid
                else:
                    hi = mid - 1
            size = lo
        font = _load_font(src, size)
        lh = line_h(font)
        keep = max(1, min(len(lines), ah // lh))
        shown = lines[:keep]
        if keep < len(lines):
            shown[-1] = shown[-1] + "..."
        out = []
        for ln in shown:
            if width(ln, font) > aw:
                cut = ln.rstrip(".")
                while cut and width(cut + "...", font) > aw:
                    cut = cut[:-1]
                ln = (cut + "...") if cut else ln[:1]
            out.append(ln)
        top = (ph - lh * len(out)) // 2
        for i, ln in enumerate(out):
            b = d.textbbox((0, 0), ln, font=font)
            x = (pw - (b[2] - b[0])) // 2 - b[0]
            d.text((x, top + i * lh), ln, font=font,
                   fill=tuple(fg[:3]) + (255,))
        if len(_NAME_TEXT_CACHE) >= 512:
            _NAME_TEXT_CACHE.clear()
        _NAME_TEXT_CACHE[ck] = img
        return img

    @staticmethod
    def _per_line_sizes(lines, src, lo, hi, aw, ah, width, line_h):
        """Per-line font sizes for a name sprite, or None when the lines
        don't fit below each other even at the smallest size `lo` (the
        uniform path then cuts them). Each line's cap is the largest size
        at which it fits the width `aw`; a common ceiling c, the largest
        whose capped sizes min(cap, c) stack within the height `ah`, then
        limits the lines whose width would let them grow past it, so the
        widest lines keep their size and the others share what is left."""
        def largest(ok, a, b):
            # The largest size in [a, b] passing `ok` (a passes or is a).
            while a < b:
                mid = (a + b + 1) // 2
                if ok(mid):
                    a = mid
                else:
                    b = mid - 1
            return a
        caps = [largest(lambda z, ln=ln: width(ln, _load_font(src, z)) <= aw,
                        lo, hi) for ln in lines]

        def stack(c):
            return sum(line_h(_load_font(src, min(cap, c))) for cap in caps)
        if stack(lo) > ah:
            return None
        c = largest(lambda z: stack(z) <= ah, lo, max(caps))
        return [min(cap, c) for cap in caps]

    def _fill_glyph(self, glyph, bw, bh, tint, opacity, box=None):
        """The glyph fallback of a stretch-mode body: on a bw x bh body, the
        glyph's drawn shape scaled up (keeping its proportions) to fill
        _GLYPH_FILL of it, centred. Returns the part inside `box` (body
        pixels, default all of it). The glyph is drawn at most
        _GLYPH_DRAW_MAX pixels high and scaled from there, so a huge body
        costs what the box shows."""
        if box is None:
            box = (0, 0, bw, bh)
        out = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]),
                        (0, 0, 0, 0))
        side = min(max(bw, bh), _GLYPH_DRAW_MAX)
        layer = self._glyph_layer(glyph, side, tint, opacity)
        ink = layer.getchannel("A").getbbox()
        if not ink:
            return out
        shape = layer.crop(ink)
        k = min(bw * _GLYPH_FILL / shape.width, bh * _GLYPH_FILL / shape.height)
        fw, fh = max(1, round(shape.width * k)), max(1, round(shape.height * k))
        sx0, sy0 = (bw - fw) // 2, (bh - fh) // 2
        ix0, iy0 = max(box[0], sx0), max(box[1], sy0)
        ix1, iy1 = min(box[2], sx0 + fw), min(box[3], sy0 + fh)
        if ix1 <= ix0 or iy1 <= iy0:
            return out
        kx, ky = shape.width / fw, shape.height / fh
        part = shape.resize(
            (ix1 - ix0, iy1 - iy0), Image.LANCZOS,
            box=((ix0 - sx0) * kx, (iy0 - sy0) * ky,
                 (ix1 - sx0) * kx, (iy1 - sy0) * ky))
        out.alpha_composite(part, (ix0 - box[0], iy0 - box[1]))
        return out

    def _glyph_layer(self, glyph, side, tint, opacity):
        """A `side`-pixel square with the glyph centred in it."""
        rgb = self._rgb(tint) or (220, 220, 220)
        a = max(0, min(255, int(opacity / 100.0 * 255)))
        layer = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        font = self.font(side, glyph)
        try:
            bbox = d.textbbox((0, 0), glyph, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            tx = (side - tw) // 2 - bbox[0]
            ty = (side - th) // 2 - bbox[1]
        except Exception:
            tx = ty = side // 4
        d.text((tx, ty), glyph, font=font, fill=(rgb[0], rgb[1], rgb[2], a))
        return layer

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

    def _draw_highlight(self, canvas, hl, ox, oy, cols, rows):
        """One highlight group: {cells: [[x, y], ...], rgb: [r, g, b],
        opacity: 0-100} — a flat translucent square on every cell."""
        cell = self.cell
        try:
            r, g, b = (max(0, min(255, int(c))) for c in hl.get("rgb", (255, 64, 64)))
        except (TypeError, ValueError):
            r, g, b = 255, 64, 64
        a = max(0, min(255, int(max(0, min(100, int(hl.get("opacity", 40)))) / 100.0 * 255)))
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        for c in hl.get("cells") or []:
            gx, gy = int(c[0]), int(c[1])
            if not (ox <= gx <= ox + cols - 1 and oy <= gy <= oy + rows - 1):
                continue
            x0, y0 = (gx - ox) * cell, (gy - oy) * cell
            d.rectangle([x0, y0, x0 + cell - 1, y0 + cell - 1], fill=(r, g, b, a))
        canvas.alpha_composite(overlay)

    def _draw_borders(self, canvas, borders, ox, oy, cols, rows,
                      voids=frozenset()):
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
                if (gx, gy) in voids:
                    continue
                ov = overrides.get(f"{gx},{gy}") or {}
                color = ov.get("color", base_color)
                op = ov.get("opacity", base_op)
                x0, y0 = (gx - ox) * cell, (gy - oy) * cell
                d.rectangle([x0, y0, x0 + cell - 1, y0 + cell - 1],
                            outline=line_rgba(color, op))
        canvas.alpha_composite(overlay)


def _label_font(cell: int):
    size = max(8, int(cell * 0.3))
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # older Pillow: load_default() takes no size
        return ImageFont.load_default()


def ruler_margin(cell: int, largest: int) -> int:
    """Margin (pixels) for coordinate labels up to `largest` at this cell
    size: room for the digits plus padding, at least half a cell."""
    digits = len(str(max(1, int(largest))))
    return max(cell // 2, int(max(8, cell * 0.3) * 0.65 * digits) + cell // 4)


def scene_dims(scene: Dict[str, Any]) -> Tuple[int, int]:
    """(cols, rows) of cells a scene renders — the viewport window when one is
    set, else the whole grid. Mirrors SceneRenderer.render's own sizing."""
    vp = scene.get("viewport")
    if isinstance(vp, dict):
        cols, rows = vp.get("w", 1), vp.get("h", 1)
    else:
        cols, rows = scene.get("grid_width", 1), scene.get("grid_height", 1)
    try:
        cols, rows = max(1, int(cols)), max(1, int(rows))
    except (TypeError, ValueError):
        return 1, 1
    if scene.get("coords"):
        # The ruler margins: at most a cell along the top, and along the
        # left a little more for three-digit row numbers.
        cols, rows = cols + 2, rows + 1
    return cols, rows


def fit_cell_size(scene: Dict[str, Any], cell: int, max_dim: int) -> int:
    """The largest cell size <= `cell` whose rendered image fits `max_dim`
    pixels on its longest side (0 = no cap). Picked BEFORE rendering: the
    canvas is cols*cell x rows*cell RGBA, so an 80x80 grid at the default
    100 px would otherwise allocate an 8000x8000 (~256 MB) image just to be
    downscaled — or, in the GUI at 4x zoom, far more."""
    cell = max(1, int(cell))
    if not max_dim:
        return cell
    if scene.get("blank"):
        return cell
    return max(1, min(cell, int(max_dim) // max(scene_dims(scene))))


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
    Raises RuntimeError if Pillow is unavailable.

    Reads the live match, so call it on the thread that owns the match. To
    keep the pixel work off an event loop, build the scene there with
    `scene_for_png` and hand only the scene to `render_scene_png` in a worker
    thread (see discord_commands)."""
    scene, cell_size = scene_for_png(match, pov_team, viewport, cell_size,
                                     max_dim)
    return render_scene_png(scene, loader, cell_size, max_dim)


def _blank_size(text: str) -> Tuple[int, int]:
    """The canvas of a blank board: wide enough for its text."""
    return max(320, 16 + 8 * len(text)), 48


def scene_for_png(match, pov_team: Optional[str] = None,
                  viewport: Optional[Tuple[int, int, int, int]] = None,
                  cell_size: Optional[int] = None,
                  max_dim: int = 1600,
                  highlights: Optional[list] = None,
                  hidden_layers: Optional[set] = None,
                  coords: Optional[bool] = None,
                  legend: bool = False, mp: Any = None
                  ) -> Tuple[Dict[str, Any], int]:
    """(scene model, cell size) for a PNG render — the part that READS THE
    MATCH. render_scene switches on the match's shared vision memo while it
    runs, so running it in a worker thread while commands mutate the match
    on the event loop could serve those commands stale sight (or leave the
    memo switched on for good). Call this on the match's own thread."""
    if not _PIL_OK:
        raise RuntimeError("graphics rendering needs Pillow (pip install Pillow).")
    if cell_size is None:
        try:
            cell_size = int(match.rules.get("sprite_cell_size", 100))
        except (TypeError, ValueError):
            cell_size = 100
    scene = match.render_scene(pov_team=pov_team, hidden_layers=hidden_layers,
                               viewport=viewport, legend=legend, mp=mp)
    if coords is not None:
        scene["coords"] = bool(coords)
    if highlights:
        scene["highlights"] = highlights
    return scene, fit_cell_size(scene, cell_size, max_dim)


def render_scene_png(scene: Dict[str, Any], loader: "SpriteLoader",
                     cell_size: int, max_dim: int = 1600) -> bytes:
    """Draw an already-built scene model to PNG bytes. Touches only the scene
    dict and the sprite loader — never the match — so it is safe to run in a
    worker thread."""
    if not _PIL_OK:
        raise RuntimeError("graphics rendering needs Pillow (pip install Pillow).")
    img = SceneRenderer(loader, cell_size).render(scene)
    # Safety net only: fit_cell_size already sized the canvas to the cap.
    if max_dim and max(img.size) > max_dim:
        scale = max_dim / float(max(img.size))
        img = img.resize((max(1, int(img.width * scale)),
                          max(1, int(img.height * scale))))
    buf = io.BytesIO()
    img.convert("RGBA").save(buf, format="PNG")
    return buf.getvalue()
