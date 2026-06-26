## gui.py (graphics surface: sprite rendering for the VTT)
#
# A desktop window that runs the SAME commands as cli.py but renders the
# match graphically (sprites) instead of as ASCII. It consumes the engine's
# pixel-agnostic render_scene() model and draws it with Pillow onto a tkinter
# canvas; the engine never loads an image.
#
# Deliberately split so the rendering logic is testable without a display:
#   - SpriteLoader : PNG-only, path-traversal-safe sprite cache (pure stdlib+PIL)
#   - SceneRenderer: render_scene() model -> a Pillow Image (pure PIL)
#   - GuiApp       : the tkinter window (command entry + canvas) — thin glue,
#                    tkinter imported lazily so the testable pieces load on a
#                    headless box.
#
# v1 scope (agreed): static sprites only (no animation), command input only
# (no mouse select/drag), whole-map render (no in-GUI pan/zoom). Discord will
# later render the same model to an image attachment.
from __future__ import annotations
import os
import asyncio
import shlex
from typing import Optional, Dict, Any, Tuple, List

try:
    from PIL import Image, ImageDraw, ImageOps, ImageColor, ImageFont
    _PIL_OK = True
except ImportError:  # Pillow is required for this surface.
    Image = None  # type: ignore
    _PIL_OK = False

from logic import MatchManager
from vtt_commands import registry

SPRITES_DIR_DEFAULT = "sprites"
ALLOWED_EXT = (".png",)
_BG_FILL = (20, 20, 24, 255)  # canvas backdrop behind everything


# ----------------------------------------------------------------------------
# Sprite loading (PNG-only, secure, cached) — no tkinter, headless-testable.
# ----------------------------------------------------------------------------
class SpriteLoader:
    """Loads + caches PNG sprites from a folder. Security: PNG files ONLY
    (extension AND magic checked), and a key can't escape the folder (no
    absolute paths, no `..` traversal). Returns RGBA Pillow Images; a
    missing / non-PNG / unsafe key caches and returns None (the renderer
    then falls back to the glyph-as-text the model carries)."""

    def __init__(self, folder: str = SPRITES_DIR_DEFAULT):
        self.folder = os.path.abspath(folder)
        self._cache: Dict[str, Optional["Image.Image"]] = {}

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
# Scene rendering (render_scene model -> Pillow Image) — headless-testable.
# ----------------------------------------------------------------------------
class SceneRenderer:
    """Draws a render_scene() model to a Pillow RGBA Image. Pure Pillow — no
    tkinter, so it renders to an off-screen image and is unit-testable on a
    headless machine."""

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
        tint_layer = Image.new("RGB", img.size, rgb)
        from PIL import ImageChops
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
        if isinstance(bg, dict):
            self._draw_background(canvas, bg, W, H)

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
    def _draw_background(self, canvas, bg, W, H):
        img = self.loader.get(bg.get("sprite"))
        if img is None:
            return
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

    def _draw_placement(self, canvas, p, ox, oy, cols, rows):
        cell = self.cell
        gx, gy = int(p.get("x", 1)), int(p.get("y", 1))
        w, h = max(1, int(p.get("w", 1))), max(1, int(p.get("h", 1)))
        # The anchor must be within the window to draw (footprint may spill).
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
            else:  # single: one sprite at the anchor cell
                canvas.alpha_composite(img.resize((cell, cell)), (x0, y0))
        else:
            # No sprite: fall back to the glyph as centered text.
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
# The tkinter window (thin glue). Imported lazily so the above is usable
# (and testable) without tkinter / a display.
# ----------------------------------------------------------------------------
class GuiCtx:
    """ReplyContext for the GUI: same wiring as CLICtx but `send` routes to
    the on-screen log, and choices use a tkinter dialog."""
    channel_key = "GUI"
    supports_color = False      # graphics surface; ANSI not used
    viewport_capable = False    # v1 renders the whole map
    cli_mutable = True
    auto_approve = True

    def __init__(self, app: "GuiApp"):
        self.app = app
        self.user_id = "gui"
        self.user_name = "gui"

    async def send(self, message: str):
        self.app.log(message)

    async def prompt_choice(self, prompt, options, lo, hi):
        from tkinter import simpledialog
        if options is not None:
            shown = "\n".join(f"{i + 1}) {o}" for i, o in enumerate(options))
            ans = simpledialog.askstring(
                "Choose", f"{prompt}\n{shown}\n(value or number; blank=cancel)")
        else:
            ans = simpledialog.askstring(
                "Choose", f"{prompt}\n(number {lo}-{hi}; blank=cancel)")
        if not ans:
            return None
        ans = ans.strip()
        if options is not None and ans.isdigit():
            i = int(ans) - 1
            if 0 <= i < len(options):
                return options[i]
        return ans


class GuiApp:
    """The window: a command Entry + an output log + a render Canvas that
    redraws the active match's render_scene after every command."""

    def __init__(self, sprites_dir: str = SPRITES_DIR_DEFAULT):
        if not _PIL_OK:
            raise RuntimeError(
                "The GUI surface needs Pillow: pip install Pillow")
        import tkinter as tk  # lazy: needs a display
        self.tk = tk
        self.mgr = MatchManager()
        self.ctx = GuiCtx(self)
        self.loader = SpriteLoader(sprites_dir)
        self.loop = asyncio.new_event_loop()
        self._photo = None  # keep a ref so Tk doesn't GC the image

        self.root = tk.Tk()
        self.root.title("FCS VTT — graphics")
        self.canvas = tk.Canvas(self.root, width=800, height=600,
                                bg="#141418", highlightthickness=0)
        self.canvas.pack(side="top", fill="both", expand=True)
        self.log_widget = tk.Text(self.root, height=8, bg="#0e0e12",
                                  fg="#d0d0d0", insertbackground="#d0d0d0")
        self.log_widget.pack(side="top", fill="x")
        self.entry = tk.Entry(self.root)
        self.entry.pack(side="bottom", fill="x")
        self.entry.bind("<Return>", self._on_enter)
        self.entry.focus_set()
        self.log("FCS VTT graphics surface. Type !help. Sprites from: "
                 f"{self.loader.folder}")

    def log(self, message: str):
        self.log_widget.insert("end", str(message) + "\n")
        self.log_widget.see("end")

    def _active_match(self):
        mid = self.mgr.active_by_channel.get(self.ctx.channel_key)
        return self.mgr.matches.get(mid) if mid else None

    def _cell_size(self, m) -> int:
        try:
            return int(m.rules.get("sprite_cell_size", 100))
        except (TypeError, ValueError):
            return 100

    def redraw(self):
        m = self._active_match()
        if m is None:
            self.canvas.delete("all")
            return
        scene = m.render_scene()
        renderer = SceneRenderer(self.loader, self._cell_size(m))
        img = renderer.render(scene)
        from PIL import ImageTk
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.config(scrollregion=(0, 0, img.width, img.height))
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

    def _on_enter(self, _event=None):
        line = self.entry.get().strip()
        self.entry.delete(0, "end")
        if not line:
            return
        if not line.startswith("!"):
            self.log("Commands must start with '!'")
            return
        try:
            parts = shlex.split(line[1:])
        except ValueError as e:
            self.log(f"❌ Parse error: {e}")
            return
        if not parts:
            return
        root, *args = parts
        try:
            self.loop.run_until_complete(
                registry.run(root, args, self.ctx, self.mgr))
        except Exception as e:
            self.log(f"❌ {e}")
        # Reload sprites the GM may have dropped in, then redraw.
        self.loader.clear()
        try:
            self.redraw()
        except Exception as e:
            self.log(f"⚠️ render error: {e}")

    def run(self):
        self.root.mainloop()


def main(sprites_dir: str = SPRITES_DIR_DEFAULT):
    if not _PIL_OK:
        print("The GUI surface needs Pillow: pip install Pillow")
        return
    try:
        app = GuiApp(sprites_dir)
    except Exception as e:
        print(f"Could not start the GUI: {e}\n"
              "(needs tkinter + a display; use cli.py for a headless box.)")
        return
    app.run()


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else SPRITES_DIR_DEFAULT)
