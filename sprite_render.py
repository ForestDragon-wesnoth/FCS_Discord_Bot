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
def _unit_style(style: Any) -> Dict[str, int]:
    """A scene's unit_style (team outline / tint rules), with the rule
    defaults for anything missing or malformed."""
    out: Dict[str, Any] = {"outline_width": 3, "outline_opacity": 100,
                           "tint_opacity": 0, "active_width": 3}
    if isinstance(style, dict):
        for k in list(out):
            try:
                out[k] = max(0, int(style.get(k, out[k])))
            except (TypeError, ValueError):
                pass
    mode = style.get("active_mode") if isinstance(style, dict) else None
    out["active_mode"] = (mode if mode in ("outline", "team_brighter", "off")
                          else "outline")
    color = style.get("active_color") if isinstance(style, dict) else None
    out["active_color"] = color if isinstance(color, str) else "white"
    return out


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
        self._unit = _unit_style(None)
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
        if not isinstance(name, str) or not name.strip():
            return None
        key = name.strip().lower()
        if key in _PALETTE_RGB:
            return _PALETTE_RGB[key]
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
        except (TypeError, ValueError):
            self._tint_fill = 40
        self._unit = _unit_style(scene.get("unit_style"))
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

        # Grid border lines: drawn ABOVE the ground/background but BELOW tiles,
        # zones, and entities (so they aid alignment without occluding content).
        borders = scene.get("borders") or {}
        if borders.get("show"):
            self._draw_borders(canvas, borders, ox, oy, cols, rows)

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
        mini._tint_fill, mini._unit, mini._base = self._tint_fill, self._unit, self._base
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
        opacity = int(p.get("opacity", 100))
        outline = None
        if img is not None and p.get("kind") == "entity":
            outline = self._rgb(p.get("tint"))
        wpx = self._outline_px()
        if outline is None or self._unit["outline_width"] <= 0:
            outline = None
        # The acting unit (active_unit_image_highlight): an outline in the
        # active colour, or its team outline brightened, sprite or glyph.
        u = self._unit
        if p.get("active") and p.get("kind") == "entity" \
                and u["active_mode"] != "off":
            if u["active_mode"] == "team_brighter":
                base = self._rgb(p.get("tint")) or (255, 255, 255)
                outline = tuple((c + 255) // 2 for c in base[:3])
            else:
                outline = self._rgb(u["active_color"]) or (255, 255, 255)
            wpx = self._outline_px(u["active_width"])
            ring_op = 100
        else:
            ring_op = None
        margin = wpx + 2 if outline is not None else 0
        # The body's pixels, and the box of them that is drawn: the window
        # plus `margin` on each side (the outline reads its neighbours).
        W, H = w * cell, h * cell
        x0, y0 = (gx - ox) * cell, (gy - oy) * cell
        box = (max(0, -x0 - margin), max(0, -y0 - margin),
               min(W, cols * cell - x0 + margin),
               min(H, rows * cell - y0 + margin))
        if box[2] <= box[0] or box[3] <= box[1]:
            return
        piece = None        # one cell's look, tiled over the box
        body = None         # the box's look, built directly
        if img is not None:
            if p.get("flip_h"):
                img = ImageOps.mirror(img)
            if p.get("flip_v"):
                img = ImageOps.flip(img)
            if p.get("kind") == "entity":
                # A unit's colour: a blended tint (team_tint_opacity) and
                # an outline around the sprite, added once the body is built.
                img = self._tint(img, p.get("tint"), self._unit["tint_opacity"])
            else:
                img = self._tint(img, p.get("tint"))
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
            glyph = p.get("glyph")
            if not (isinstance(glyph, str) and glyph):
                # A coloured zone / tile with no sprite or glyph: a
                # translucent square of its colour (ASCII tints the `.`).
                # Only those: a corpse's or overlay's tint recolours its
                # sprite, so a missing PNG there draws nothing.
                if p.get("kind") not in ("zone", "tile"):
                    return
                rgb = self._rgb(p.get("tint"))
                fill = self._tint_fill * opacity // 100
                if rgb is None or fill <= 0:
                    return
                a = max(0, min(255, int(fill / 100.0 * 255)))
                piece = Image.new("RGBA", (cell, cell), tuple(rgb[:3]) + (a,))
                mode = "tile"
            else:
                gtint = p.get("glyph_tint", p.get("tint"))
                gop = int(p.get("glyph_opacity", opacity))
                if mode == "stretch":
                    body = self._fill_glyph(glyph, W, H, gtint, gop, box)
                else:
                    piece = self._glyph_layer(glyph, cell, gtint, gop)
        if body is None:
            body = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]),
                             (0, 0, 0, 0))
            for dy in range(box[1] // cell, (box[3] - 1) // cell + 1):
                for dx in range(box[0] // cell, (box[2] - 1) // cell + 1):
                    px, py = dx * cell - box[0], dy * cell - box[1]
                    body.alpha_composite(piece, (max(0, px), max(0, py)),
                                         (max(0, -px), max(0, -py)))
        if outline is not None:
            body = self._outline(body, outline, wpx, ring_op)
        bx, by = x0 + box[0], y0 + box[1]
        sx, sy = max(0, -bx), max(0, -by)
        canvas.alpha_composite(body, (bx + sx, by + sy), (sx, sy))

    def _outline_px(self, width: Optional[int] = None) -> int:
        """team_outline_width (or `width`) at sprite_cell_size cells, scaled
        to this cell size, in pixels."""
        w = self._unit["outline_width"] if width is None else width
        return max(1, round(w * self.cell / max(1, self._base)))

    def _outline(self, body, rgb, wpx: Optional[int] = None,
                 opacity: Optional[int] = None):
        """`body` with an outline in `rgb` around its drawn shape:
        team_outline_width pixels at sprite_cell_size cells, scaled to this
        cell size. Drawn outside the shape where the body has transparent
        room, and just inside the body's edge where the shape reaches it."""
        from PIL import ImageFilter, ImageChops
        u = self._unit
        wpx = wpx or self._outline_px()
        alpha = body.getchannel("A").point(lambda v: 255 if v >= 128 else 0)
        pad = Image.new("L", (body.width + 2 * wpx, body.height + 2 * wpx), 0)
        pad.paste(alpha, (wpx, wpx))
        grown = pad.filter(ImageFilter.MaxFilter(2 * wpx + 1))
        outer = ImageChops.subtract(grown, pad).crop(
            (wpx, wpx, wpx + body.width, wpx + body.height))
        # The shape's pixels within wpx of the body's own border.
        frame = Image.new("L", body.size, 255)
        if body.width > 2 * wpx and body.height > 2 * wpx:
            ImageDraw.Draw(frame).rectangle(
                [wpx, wpx, body.width - 1 - wpx, body.height - 1 - wpx], fill=0)
        inner = ImageChops.multiply(alpha, frame)
        ring = ImageChops.lighter(outer, inner)
        op = u["outline_opacity"] if opacity is None else opacity
        a = max(0, min(255, int(op / 100.0 * 255)))
        ring = ring.point(lambda v: a if v else 0)
        layer = Image.new("RGBA", body.size, tuple(rgb[:3]) + (0,))
        layer.putalpha(ring)
        out = body.copy()
        out.alpha_composite(layer)
        return out

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
