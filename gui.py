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

# The sprite loading + scene-to-image rendering live in sprite_render.py so the
# Discord image surface can reuse them without depending on tkinter. gui.py is
# the tkinter glue around them.
from sprite_render import (
    _PIL_OK, SpriteLoader, SceneRenderer, SPRITES_DIR_DEFAULT,
)

from logic import MatchManager
from vtt_commands import registry


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
    redraws the active match's render_scene after every command. The canvas
    supports in-GUI PAN (scrollbars, left-drag, arrow keys) and ZOOM (mouse
    wheel, +/- keys, on-screen buttons) — the camera is GUI-local and does not
    touch the engine's per-channel viewport."""

    _ZOOM_MIN = 0.25
    _ZOOM_MAX = 4.0
    _ZOOM_STEP = 1.25

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
        self._zoom = 1.0

        self.root = tk.Tk()
        self.root.title("FCS VTT — graphics")

        # A small toolbar (zoom buttons + readout) above the canvas.
        bar = tk.Frame(self.root, bg="#0e0e12")
        bar.pack(side="top", fill="x")
        tk.Button(bar, text="–", width=2,
                  command=lambda: self._zoom_by(1 / self._ZOOM_STEP)).pack(side="left")
        tk.Button(bar, text="+", width=2,
                  command=lambda: self._zoom_by(self._ZOOM_STEP)).pack(side="left")
        tk.Button(bar, text="Reset", command=self._zoom_reset).pack(side="left")
        self.zoom_label = tk.Label(bar, text="100%", bg="#0e0e12", fg="#d0d0d0")
        self.zoom_label.pack(side="left", padx=6)
        tk.Label(bar, text="(drag to pan · wheel/Ctrl+wheel to zoom)",
                 bg="#0e0e12", fg="#707070").pack(side="left")

        # ---- Widget creation ----
        # Canvas + scrollbars (the map). Packed LAST (see below) so it's the
        # widget that gives up / absorbs space on resize.
        frame = tk.Frame(self.root)
        self.canvas = tk.Canvas(frame, width=800, height=420,
                                bg="#141418", highlightthickness=0)
        vbar = tk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        hbar = tk.Scrollbar(frame, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        vbar.pack(side="right", fill="y")
        hbar.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)

        # Output log: READ-ONLY (disabled except while we insert) so it can't
        # be mistaken for / typed into as the input box.
        self.log_widget = tk.Text(self.root, height=6, bg="#0e0e12",
                                  fg="#d0d0d0", insertbackground="#d0d0d0",
                                  state="disabled")

        # Command input: a MULTI-LINE box so a pasted block of commands runs
        # line-by-line (like the CLI / a Discord !batch). Enter runs every
        # non-empty line in order; Shift+Enter inserts a literal newline.
        inbar = tk.Frame(self.root)
        tk.Button(inbar, text="Run", command=self._run_input).pack(side="right")
        self.entry = tk.Text(inbar, height=6, bg="#1a1a20", fg="#e0e0e0",
                             insertbackground="#e0e0e0", wrap="word")
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", self._on_enter)
        self.entry.bind("<Shift-Return>", lambda e: None)  # literal newline
        self.entry.bind("<Control-Return>", self._on_enter)
        self.entry.focus_set()

        # ---- Layout / resize priority ----
        # Pack order = clipping priority (earliest packed keeps its space). We
        # want, when the window is too short: the INPUT first-class (always
        # visible), the LOG second-class, and the MAP to shrink to fit. So pack
        # input then log at the bottom (reserved first), then the canvas frame
        # last with expand so it absorbs/yields the remaining space.
        inbar.pack(side="bottom", fill="x")   # very bottom, reserved first
        self.log_widget.pack(side="bottom", fill="x")  # above input, second
        frame.pack(side="top", fill="both", expand=True)  # map fills the rest
        # Don't let the window grow taller than these reservations need.
        self.root.minsize(480, 300)

        # Pan: left-drag (scan), arrow keys.
        self.canvas.bind("<ButtonPress-1>",
                         lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind("<B1-Motion>",
                         lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        # Zoom: mouse wheel (Windows/Mac <MouseWheel>; Linux Button-4/5).
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", self._on_wheel)
        self.canvas.bind("<Button-5>", self._on_wheel)
        # Keyboard camera (when the canvas has focus).
        self.root.bind("<Control-plus>", lambda e: self._zoom_by(self._ZOOM_STEP))
        self.root.bind("<Control-equal>", lambda e: self._zoom_by(self._ZOOM_STEP))
        self.root.bind("<Control-minus>", lambda e: self._zoom_by(1 / self._ZOOM_STEP))
        self.canvas.bind("<Up>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind("<Down>", lambda e: self.canvas.yview_scroll(1, "units"))
        self.canvas.bind("<Left>", lambda e: self.canvas.xview_scroll(-1, "units"))
        self.canvas.bind("<Right>", lambda e: self.canvas.xview_scroll(1, "units"))
        self.log("FCS VTT graphics surface. Type !help (one command per line; "
                 "Enter runs all lines, Shift+Enter for a newline). Sprites "
                 f"from: {self.loader.folder}")

    def log(self, message: str):
        self.log_widget.config(state="normal")
        self.log_widget.insert("end", str(message) + "\n")
        self.log_widget.see("end")
        self.log_widget.config(state="disabled")

    def _active_match(self):
        mid = self.mgr.active_by_channel.get(self.ctx.channel_key)
        return self.mgr.matches.get(mid) if mid else None

    def _cell_size(self, m) -> int:
        try:
            base = int(m.rules.get("sprite_cell_size", 100))
        except (TypeError, ValueError):
            base = 100
        return max(1, int(base * self._zoom))

    # -- zoom controls ---------------------------------------------------
    def _zoom_by(self, factor: float):
        self._zoom = max(self._ZOOM_MIN, min(self._ZOOM_MAX, self._zoom * factor))
        self.zoom_label.config(text=f"{int(self._zoom * 100)}%")
        try:
            self.redraw()
        except Exception as e:
            self.log(f"⚠️ render error: {e}")

    def _zoom_reset(self):
        self._zoom = 1.0
        self._zoom_by(1.0)

    def _on_wheel(self, event):
        # Normalize across platforms: <MouseWheel>.delta (±120 steps) on
        # Windows/Mac, Button-4 (up) / Button-5 (down) on Linux.
        up = getattr(event, "delta", 0) > 0 or getattr(event, "num", 0) == 4
        self._zoom_by(self._ZOOM_STEP if up else 1 / self._ZOOM_STEP)

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
        # Bound to <Return>: run all lines, and suppress the default newline
        # insertion in the Text widget (return "break"). Shift+Enter keeps the
        # default (a literal newline) because it's bound separately.
        self._run_input()
        return "break"

    def _run_input(self):
        block = self.entry.get("1.0", "end")
        self.entry.delete("1.0", "end")
        lines = [ln.strip() for ln in block.splitlines()]
        ran = False
        for line in lines:
            if not line:
                continue
            ran = True
            self._run_line(line)
        if not ran:
            return
        # Reload sprites the GM may have dropped in, then redraw once.
        self.loader.clear()
        try:
            self.redraw()
        except Exception as e:
            self.log(f"⚠️ render error: {e}")

    def _run_line(self, line: str):
        self.log(f"> {line}")
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
