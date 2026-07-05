"""Toolpath preview canvas (Cairo), theme-aware.

Drawn in a fixed "board view": the bed is shown the same way every time, and the Home
corner (machine origin) sits at the physical corner chosen in settings, with the X/Y
axes pointing INTO the board. Changing Home immediately moves the axes to that corner.

  - grid (theme gray) with a value+unit label at every gridline (machine mm from Home),
  - axes from the Home corner: X red, Y green, each ending in an arrowhead + name,
  - blue toolpath: burn solid, travel dashed, return-to-Home dashed orange (no arrows).

Mouse: scroll = zoom (toward cursor), left-drag = pan, right-drag = rotate, double-click reset.
"""

from __future__ import annotations

import math
from typing import List, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from .. import constants as C
from .style import ACCENT

Point = Tuple[float, float]


def _hex_rgb(h: str) -> Tuple[float, float, float]:
    n = int(h.lstrip("#"), 16)
    return ((n >> 16 & 255) / 255.0, (n >> 8 & 255) / 255.0, (n & 255) / 255.0)


# Design palette (jas GRBL.dc.html): red X / green Y axes, accent origin dot, orange
# return-to-Home path.
AXIS_X_RGB = _hex_rgb("#f85149")   # red
AXIS_Y_RGB = _hex_rgb("#3fb950")   # green
HOME_RGB = (1.0, 0.55, 0.0)        # orange - return-to-Home path
ORIGIN_RGB = _hex_rgb(ACCENT)      # accent - Home origin dot


def _merge_runs(segments: List[Tuple]):
    runs = []
    cur = None
    cur_kind = None
    for seg in segments:
        kind, a, b = seg[0], seg[1], seg[2]
        if cur is not None and kind == cur_kind and \
                math.hypot(cur[-1][0] - a[0], cur[-1][1] - a[1]) < 1e-6:
            cur.append(b)
        else:
            if cur is not None and len(cur) >= 2:
                runs.append((cur_kind, cur))
            cur = [a, b]
            cur_kind = kind
    if cur is not None and len(cur) >= 2:
        runs.append((cur_kind, cur))
    return runs


def _nice_step(target: float) -> float:
    if target <= 0:
        return 10.0
    mag = 10 ** math.floor(math.log10(target))
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= target:
            return m * mag
    return 10 * mag


def _mix(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


class PreviewCanvas(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.board_w = 400.0
        self.board_h = 400.0
        self.home = C.HOME_BOTTOM_LEFT
        self.segments: List[Tuple] = []
        # Two-tier cache, both bypassing per-frame work:
        #   _runs_board : merged runs already in board coords, grouped by style.
        #                 Rebuilt only when geometry (segments/home/board) changes.
        #   _path_cache : baked cairo.Path per group, decimated for the zoom level
        #                 it was built at. Rebuilt only when scale changes enough
        #                 (hysteresis), so pan/drag/rotate reuse it as-is.
        self._runs_board = None
        self._path_cache = None
        self._baked_scale = 0.0
        self.scale = 1.0
        self.pan = [0.0, 0.0]
        self.angle = 0.0
        self._fitted = False
        self._drag = None
        self._btn = 0
        self._W = 1
        self._H = 1
        self.set_size_request(560, 440)
        self.set_has_window(True)
        self.add_events(
            Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("draw", self._on_draw)
        self.connect("scroll-event", self._on_scroll)
        self.connect("button-press-event", self._on_press)
        self.connect("button-release-event", self._on_release)
        self.connect("motion-notify-event", self._on_motion)

    # -------------------------------------------------------------- API
    def set_machine(self, width: float, height: float, home: str) -> None:
        refit = (width != self.board_w or height != self.board_h)
        home_changed = (home != self.home)
        self.board_w = max(1.0, width)
        self.board_h = max(1.0, height)
        self.home = home
        if refit:
            self._fitted = False
        # _b() (machine->board) depends on home + board size, so the cached
        # board-space geometry becomes stale when either changes.
        if refit or home_changed:
            self._invalidate_geometry()
        self.queue_draw()

    def set_program(self, program) -> None:
        self.segments = list(program.segments) if program else []
        self._invalidate_geometry()
        self.queue_draw()

    def clear_paths(self) -> None:
        self.segments = []
        self._invalidate_geometry()
        self.queue_draw()

    def _invalidate_geometry(self) -> None:
        self._runs_board = None
        self._path_cache = None

    def reset_view(self) -> None:
        self._fitted = False
        self.queue_draw()

    # -------------------------------------------------------- machine->board
    def _b(self, mx: float, my: float) -> Point:
        """Machine coords (origin = Home, +X/+Y into board) -> board view
        (mm, x right, y DOWN; bed top-left at (0,0)). Places Home at its corner."""
        w, h = self.board_w, self.board_h
        if self.home == C.HOME_BOTTOM_LEFT:
            return (mx, h - my)
        if self.home == C.HOME_TOP_LEFT:
            return (mx, my)
        if self.home == C.HOME_BOTTOM_RIGHT:
            return (w - mx, h - my)
        if self.home == C.HOME_TOP_RIGHT:
            return (w - mx, my)
        return (mx, my)

    # -------------------------------------------------------------- events
    def _on_scroll(self, _w, event):
        if event.direction == Gdk.ScrollDirection.UP:
            f = 1.12
        elif event.direction == Gdk.ScrollDirection.DOWN:
            f = 1 / 1.12
        else:
            _ok, _dx, dy = event.get_scroll_deltas()
            f = (1 / 1.12) if dy > 0 else 1.12
        w = self.get_allocated_width()
        h = self.get_allocated_height()
        pivx = w / 2 + self.pan[0]
        pivy = h / 2 + self.pan[1]
        self.pan[0] = event.x - f * (event.x - pivx) - w / 2
        self.pan[1] = event.y - f * (event.y - pivy) - h / 2
        self.scale *= f
        self.queue_draw()
        return True

    def _on_press(self, _w, event):
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            self.reset_view()
            return True
        self._drag = (event.x, event.y)
        self._btn = event.button
        return True

    def _on_release(self, _w, _event):
        self._drag = None
        return True

    def _on_motion(self, _w, event):
        if self._drag is None:
            return False
        dx = event.x - self._drag[0]
        dy = event.y - self._drag[1]
        if self._btn == 3:
            self.angle += dx * 0.01
        else:
            self.pan[0] += dx
            self.pan[1] += dy
        self._drag = (event.x, event.y)
        self.queue_draw()
        return True

    # -------------------------------------------------------------- helpers
    def _fit(self, w, h):
        m = 44.0
        sx = (w - 2 * m) / self.board_w
        sy = (h - 2 * m) / self.board_h
        self.scale = max(0.05, min(sx, sy))
        self.pan = [0.0, 0.0]
        self.angle = 0.0
        self._fitted = True

    def _lw(self, px):
        return px / self.scale

    def _b2s(self, bx, by):
        """Board-view point -> screen pixels (matches the Cairo CTM, no Y flip)."""
        x = (bx - self.board_w / 2.0) * self.scale
        y = (by - self.board_h / 2.0) * self.scale
        ca, sa = math.cos(self.angle), math.sin(self.angle)
        return (self._W / 2 + self.pan[0] + x * ca - y * sa,
                self._H / 2 + self.pan[1] + x * sa + y * ca)

    def _m2s(self, mx, my):
        return self._b2s(*self._b(mx, my))

    def _theme(self):
        # Fixed dark palette from the design mock (jas GRBL.dc.html): the dialog is
        # committed to a dark console look, so the canvas no longer mirrors the GTK
        # theme. Near-black bed, faint grid, blue burn / slate travel toolpath.
        bg = _hex_rgb("#080b0f")
        return {
            "bg": bg,
            "grid": _hex_rgb("#161d25"),
            "border": _hex_rgb("#2a3342"),
            "text": _hex_rgb("#5c6672"),
            "burn": _hex_rgb("#58a6ff"),
            "travel": _mix(bg, _hex_rgb("#58a6ff"), 0.5),
        }

    # -------------------------------------------------------------- draw
    def _on_draw(self, _w, cr):
        self._W = self.get_allocated_width()
        self._H = self.get_allocated_height()
        if not self._fitted:
            self._fit(self._W, self._H)
        th = self._theme()

        cr.set_source_rgb(*th["bg"])
        cr.paint()

        cr.save()
        cr.translate(self._W / 2 + self.pan[0], self._H / 2 + self.pan[1])
        cr.rotate(self.angle)
        cr.scale(self.scale, self.scale)            # board view is already y-down
        cr.translate(-self.board_w / 2, -self.board_h / 2)
        step = _nice_step(max(self.board_w, self.board_h) / 16.0)
        self._draw_grid(cr, th, step)
        self._draw_axes(cr, th)
        self._draw_paths(cr, th)
        cr.restore()

        self._draw_labels(cr, th, step)
        self._draw_help(cr, th)
        return False

    def _moveto(self, cr, mx, my):
        bx, by = self._b(mx, my)
        cr.move_to(bx, by)

    def _lineto(self, cr, mx, my):
        bx, by = self._b(mx, my)
        cr.line_to(bx, by)

    def _draw_grid(self, cr, th, step):
        w, h = self.board_w, self.board_h
        cr.set_line_width(self._lw(1))
        cr.set_source_rgb(*th["grid"])
        x = 0.0
        while x <= w + 1e-6:
            self._moveto(cr, x, 0)
            self._lineto(cr, x, h)
            x += step
        y = 0.0
        while y <= h + 1e-6:
            self._moveto(cr, 0, y)
            self._lineto(cr, w, y)
            y += step
        cr.stroke()
        cr.set_source_rgb(*th["border"])
        cr.set_line_width(self._lw(1.4))
        for i, (mx, my) in enumerate([(0, 0), (w, 0), (w, h), (0, h)]):
            (self._moveto if i == 0 else self._lineto)(cr, mx, my)
        cr.close_path()
        cr.stroke()

    def _arrow(self, cr, tip_m, dir_m, color):
        """Filled arrowhead at machine point tip_m, pointing along machine dir_m."""
        aw = self._lw(9)
        tx, ty = self._b(*tip_m)
        # direction in board view
        ax, ay = self._b(tip_m[0] + dir_m[0], tip_m[1] + dir_m[1])
        dx, dy = ax - tx, ay - ty
        d = math.hypot(dx, dy) or 1.0
        dx, dy = dx / d, dy / d
        px, py = -dy, dx
        cr.set_source_rgb(*color)
        cr.move_to(tx, ty)
        cr.line_to(tx - dx * aw + px * aw * 0.5, ty - dy * aw + py * aw * 0.5)
        cr.line_to(tx - dx * aw - px * aw * 0.5, ty - dy * aw - py * aw * 0.5)
        cr.close_path()
        cr.fill()

    def _draw_axes(self, cr, th):
        w, h = self.board_w, self.board_h
        cr.set_line_width(self._lw(2.0))
        cr.set_source_rgb(*AXIS_X_RGB)
        self._moveto(cr, 0, 0)
        self._lineto(cr, w, 0)
        cr.stroke()
        self._arrow(cr, (w, 0), (1, 0), AXIS_X_RGB)
        cr.set_source_rgb(*AXIS_Y_RGB)
        self._moveto(cr, 0, 0)
        self._lineto(cr, 0, h)
        cr.stroke()
        self._arrow(cr, (0, h), (0, 1), AXIS_Y_RGB)
        cr.set_source_rgb(*ORIGIN_RGB)
        bx, by = self._b(0, 0)
        cr.arc(bx, by, self._lw(4), 0, 2 * math.pi)
        cr.fill()

    def _build_runs_board(self):
        """Merge segments into runs and transform to board coords ONCE per
        geometry change. Groups by draw style so each can be stroked in one go.
        Independent of zoom/pan (that is the Cairo CTM's job)."""
        runs = _merge_runs(self.segments)
        last_travel = -1
        for i, (kind, _pts) in enumerate(runs):
            if kind == "travel":
                last_travel = i
        groups = {"burn": [], "travel": [], "home": []}
        b = self._b
        for i, (kind, pts) in enumerate(runs):
            g = "burn" if kind == "burn" else ("home" if i == last_travel else "travel")
            groups[g].append([b(p[0], p[1]) for p in pts])
        self._runs_board = groups

    @staticmethod
    def _decimate(poly, tol):
        """Drop points closer than `tol` (board units) to the last kept point.
        Endpoints are always kept. O(n) radial-distance simplification: at a
        sub-pixel tolerance the result is visually identical but far cheaper to
        stroke when zoomed out."""
        n = len(poly)
        if n <= 2 or tol <= 0.0:
            return poly
        t2 = tol * tol
        out = [poly[0]]
        lx, ly = poly[0]
        for i in range(1, n - 1):
            x, y = poly[i]
            dx, dy = x - lx, y - ly
            if dx * dx + dy * dy >= t2:
                out.append((x, y))
                lx, ly = x, y
        out.append(poly[n - 1])
        return out

    def _bake_paths(self, cr):
        """Bake one cairo.Path per group, decimated for the current zoom level.
        Must run while the board CTM is active so stored coords are board-space;
        append_path re-renders them under each frame's CTM."""
        tol = 0.4 / self.scale  # ~0.4 px collapsed away at the current zoom
        cache = {}
        for g, polys in self._runs_board.items():
            cr.new_path()
            for poly in polys:
                dp = self._decimate(poly, tol)
                cr.move_to(*dp[0])
                for p in dp[1:]:
                    cr.line_to(*p)
            cache[g] = cr.copy_path()
        cr.new_path()
        self._path_cache = cache
        self._baked_scale = self.scale

    def _draw_paths(self, cr, th):
        if not self.segments:
            return
        if self._runs_board is None:
            self._build_runs_board()
        # Re-bake only when the zoom has moved enough that the decimation
        # tolerance is materially off; pan/drag/rotate keep the same baked path.
        ratio = self.scale / self._baked_scale if self._baked_scale else 0.0
        if self._path_cache is None or not (0.625 <= ratio <= 1.6):
            self._bake_paths(cr)
        cache = self._path_cache

        cr.set_dash([])
        cr.set_line_width(self._lw(1.4))
        cr.set_source_rgb(*th["burn"])
        cr.new_path()
        cr.append_path(cache["burn"])
        cr.stroke()

        cr.set_dash([self._lw(3), self._lw(3)])
        cr.set_line_width(self._lw(0.8))
        cr.set_source_rgb(*th["travel"])
        cr.new_path()
        cr.append_path(cache["travel"])
        cr.stroke()

        cr.set_dash([self._lw(4), self._lw(4)])
        cr.set_line_width(self._lw(1.0))
        cr.set_source_rgb(*HOME_RGB)
        cr.new_path()
        cr.append_path(cache["home"])
        cr.stroke()

        cr.set_dash([])

    def _draw_labels(self, cr, th, step):
        w, h = self.board_w, self.board_h
        cr.set_source_rgb(*th["text"])
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(9)

        def text(s, sx, sy, ax=0.0, ay=0.0):
            ext = cr.text_extents(s)
            cr.move_to(sx - ext.width * ax, sy + ext.height * ay)
            cr.show_text(s)

        # Label only every other gridline (starting at 0) to reduce clutter.
        i = 0
        x = 0.0
        while x <= w + 1e-6:
            if i % 2 == 0:
                sx, sy = self._m2s(x, 0)
                text("%gmm" % x, sx, sy + 14, ax=0.5)
            x += step
            i += 1
        i = 0
        y = 0.0
        while y <= h + 1e-6:
            # y=0 is already shown by the X axis; skip it to avoid overlap at the origin.
            if i % 2 == 0 and y > 1e-6:
                sx, sy = self._m2s(0, y)
                text("%gmm" % y, sx - 6, sy + 4, ax=1.0)
            y += step
            i += 1
        cr.set_font_size(12)
        cr.set_source_rgb(*AXIS_X_RGB)
        sx, sy = self._m2s(w, 0)
        text("X", sx + 12, sy + 4, ax=0.5)
        cr.set_source_rgb(*AXIS_Y_RGB)
        sx, sy = self._m2s(0, h)
        text("Y", sx + 6, sy - 8, ax=0.5)

    def _draw_help(self, cr, th):
        cr.set_source_rgb(*th["text"])
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(10)
        text = "Scroll: zoom   Drag: pan   Right-drag: rotate   Double-click: reset"
        # Centre the hint horizontally along the bottom of the canvas.
        extents = cr.text_extents(text)
        x = (self._W - extents.width) / 2 - extents.x_bearing
        cr.move_to(max(10, x), self._H - 10)
        cr.show_text(text)
