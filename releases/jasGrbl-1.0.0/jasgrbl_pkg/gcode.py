"""G-code generation: build toolpaths from layers, order them, emit GRBL G-code."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from . import constants as C
from . import geometry as G
from .config import LayerSetting
from .fills import FillParams, auto_select, get_fill

Point = G.Point
Polyline = G.Polyline
LogFn = Callable[[str, str], None]


@dataclass
class GenOptions:
    version: str = "0.0.0"
    timestamp: str = ""           # caller supplies (scripts cannot read the clock)
    s_max: int = C.DEFAULT_S_MAX
    laser_mode: str = C.LASER_DYNAMIC
    mode: str = C.MODE_ENGRAVING        # engraving (laser) | plotter (servo pen)
    park_home: bool = True
    emit_arcs: bool = True              # fit curves to G2/G3 arcs instead of many G1 lines
    arc_tolerance: float = 0.06         # mm: max deviation of a fitted arc from the path
    swap_xy: bool = False               # HPGL only: transpose X/Y (transposed-axis cutters)


@dataclass
class GcodeProgram:
    lines: List[str] = field(default_factory=list)
    segments: List[Tuple[str, Point, Point]] = field(default_factory=list)  # (kind, p0, p1)

    def line(self, text: str) -> None:
        self.lines.append(text)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def burn_length(self) -> float:
        return sum(G._dist(a, b) for kind, a, b in self.segments if kind == "burn")

    def travel_length(self) -> float:
        return sum(G._dist(a, b) for kind, a, b in self.segments if kind == "travel")


def _fmt(v: float) -> str:
    s = "%.3f" % v
    # trim trailing zeros but keep at least one digit
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


# ---------------------------------------------------------------------------
# Toolpath construction
# ---------------------------------------------------------------------------


def _shade_spacing(shape, base: float, shade_density: bool) -> float:
    """Per-shape line spacing from the fill-colour luminance (doc 03 s2).

    ``base`` is the spacing for a fully-dark fill; a lighter colour widens it up to
    SHADE_MAX_MULT, so darker regions read denser and lighter regions sparser."""
    if not shade_density:
        return base
    lum = max(0.0, min(1.0, getattr(shape, "fill_gray", 0.0)))
    return base * (1.0 + lum * (C.SHADE_MAX_MULT - 1.0))


def build_layer_toolpaths(layer, setting: LayerSetting, machine: G.MachineSpace,
                          fill_type: str, fill_params: FillParams) -> List[Polyline]:
    """Return burn polylines (machine mm) for one layer."""
    auto = (fill_type == C.FILL_AUTO)
    strategy = None if auto else get_fill(fill_type)
    burns: List[Polyline] = []

    for shape in layer.shapes:
        mpoly = [[machine.to_machine(x, y) for (x, y) in sp] for sp in shape.subpaths]
        if not mpoly:
            continue
        rings = [pl for pl in mpoly if len(pl) >= 2]

        # Stroke Text mode: engrave each filled region as its single centerline stroke
        # (one pass down the middle) instead of filling/outlining it.
        if setting.stroke_text:
            if shape.has_fill and rings:
                try:
                    from .centerline import centerline
                    strokes = centerline(rings, log=fill_params.log)
                    if strokes:
                        burns.extend(strokes)
                    else:
                        # fallback: keep the outline so nothing silently disappears
                        burns.extend(rings)
                except Exception as exc:
                    fill_params.warn("centerline failed on %s (%s); using outline"
                                     % (shape.element_id, exc))
                    burns.extend(rings)
            elif shape.has_stroke:
                # already a single open stroke - emit as-is
                for pl in rings:
                    burns.append(pl)
            continue

        # Normal mode: outline (stroke) and/or fill.
        if shape.has_stroke:
            for pl in mpoly:
                if len(pl) >= 2:
                    burns.append(pl)
        if shape.has_fill and rings:
            # Shade mode widens hatch spacing with lightness, so edges and thin
            # features narrower than the (now wider) spacing would receive no hatch
            # line and vanish. Trace the region's contour too so the outline is always
            # preserved. Skip when the shape already draws its own stroke (emitted
            # above) to avoid burning the same outline twice.
            if fill_params.shade_density and not shape.has_stroke:
                burns.extend(rings)
            try:
                sp = _shade_spacing(shape, fill_params.spacing, fill_params.shade_density)
                if auto:
                    name, angle = auto_select(rings, sp)
                    p = FillParams(spacing=sp, angle=angle, log=fill_params.log)
                    burns.extend(get_fill(name).generate(rings, p))
                else:
                    p = FillParams(spacing=sp, angle=fill_params.angle, log=fill_params.log)
                    burns.extend(strategy.generate(rings, p))
            except Exception as exc:  # never let one shape kill the job
                fill_params.warn("fill failed on %s (%s); skipped" % (shape.element_id, exc))
    return burns


# A closed chain's endpoints coincide *exactly* (fill rings are closed with a
# duplicated first point; test squares repeat their first vertex). Use a tight
# epsilon so only truly-closed rings are re-rooted - treating a near-closed OPEN
# stroke as closed would silently drop its end gap, which is never acceptable.
CLOSE_EPS = 1e-6
# Above this many chains the O(n^2) nearest-neighbour scan is replaced by an
# O(n log n) boustrophedon sweep so a dense fill can't freeze the UI (doc 11 s5).
ORDER_NN_CAP = 2000


def _is_closed(pl: Polyline) -> bool:
    return len(pl) >= 4 and G._dist(pl[0], pl[-1]) < CLOSE_EPS


def _access_dist(pl: Polyline, cur: Point) -> float:
    """Distance from cur to the cheapest entry point of pl.

    Closed rings may be entered at ANY vertex (loop re-rooting, doc 07 s3), so
    the access cost is the nearest vertex. Open chains may only be entered at an
    endpoint (optionally reversed)."""
    if _is_closed(pl):
        return min(G._dist(cur, p) for p in pl[:-1])
    return min(G._dist(cur, pl[0]), G._dist(cur, pl[-1]))


def _orient_to(pl: Polyline, cur: Point) -> Polyline:
    """Return pl re-rooted (closed) or reversed (open) so it starts nearest cur.
    Total traced length is preserved - only the entry point / direction changes."""
    if _is_closed(pl):
        core = pl[:-1]
        k = min(range(len(core)), key=lambda i: G._dist(cur, core[i]))
        rot = core[k:] + core[:k]
        rot.append(rot[0])          # keep the ring closed
        return rot
    if G._dist(cur, pl[-1]) < G._dist(cur, pl[0]):
        return list(reversed(pl))
    return pl


def order_polylines(polylines: List[Polyline], start: Point) -> Tuple[List[Polyline], Point]:
    """Greedy nearest-neighbour ordering with loop re-rooting (doc 07 s3).

    Each step picks the chain whose cheapest entry point is nearest the tool, then
    orients it (re-root a closed ring to that vertex, or reverse an open chain).
    Returns (ordered, end_point)."""
    remaining = [pl for pl in polylines if len(pl) >= 2]
    if len(remaining) > ORDER_NN_CAP:
        return _order_spatial(remaining, start)
    ordered: List[Polyline] = []
    cur = start
    while remaining:
        best_i = min(range(len(remaining)), key=lambda i: _access_dist(remaining[i], cur))
        pl = _orient_to(remaining.pop(best_i), cur)
        ordered.append(pl)
        cur = pl[-1]
    return ordered, cur


def _order_spatial(polylines: List[Polyline], start: Point) -> Tuple[List[Polyline], Point]:
    """O(n log n) fallback for very dense jobs: a boustrophedon sweep over the
    chains' entry points (sort into horizontal bands, alternate X direction), then
    orient each chain to the running cursor. Not travel-optimal, but bounded and
    far better than document order."""
    def anchor(pl: Polyline) -> Point:
        return pl[0]
    ys = [anchor(pl)[1] for pl in polylines]
    ymin, ymax = min(ys), max(ys)
    bands = max(1, int(math.sqrt(len(polylines))))
    span = (ymax - ymin) or 1.0
    band_h = span / bands

    def key(pl: Polyline):
        ax, ay = anchor(pl)
        b = min(bands - 1, int((ay - ymin) / band_h)) if band_h > 0 else 0
        return (b, ax if b % 2 == 0 else -ax)

    ordered_src = sorted(polylines, key=key)
    ordered: List[Polyline] = []
    cur = start
    for pl in ordered_src:
        pl = _orient_to(pl, cur)
        ordered.append(pl)
        cur = pl[-1]
    return ordered, cur


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def _emit_burn_moves(prog: GcodeProgram, pl: Polyline, options: GenOptions) -> None:
    """Emit G1/G2/G3 for one burn polyline, arc-fitting curves when enabled.

    Coordinates are already in the machine frame (MachineSpace.to_machine has applied
    the Home-corner mirror/flip), so they are written as-is - no extra rotation."""
    if options.emit_arcs and len(pl) >= 3:
        from .arcs import fit_arcs
        prev = pl[0]
        for mv in fit_arcs(pl, options.arc_tolerance):
            if mv[0] == "line":
                end = mv[1]
                prog.line("G1 X%s Y%s" % (_fmt(end[0]), _fmt(end[1])))
            else:
                _, end, center, cw, _r = mv
                code = "G2" if cw else "G3"
                i_off = center[0] - prev[0]
                j_off = center[1] - prev[1]
                prog.line("%s X%s Y%s I%s J%s"
                          % (code, _fmt(end[0]), _fmt(end[1]), _fmt(i_off), _fmt(j_off)))
            prev = end
    else:
        for pt in pl[1:]:
            prog.line("G1 X%s Y%s" % (_fmt(pt[0]), _fmt(pt[1])))


# A "block" is one enabled layer's ordered toolpaths, ready to be emitted by either
# the GRBL or the HPGL back-end. Planning (fills, ordering) is shared so both
# outputs describe exactly the same motion and the preview matches both.
LayerBlock = Tuple[str, LayerSetting, List[Polyline]]  # (label, setting, ordered polylines)


def plan_toolpaths(layer_settings: List[Tuple[object, LayerSetting]],
                   machine: G.MachineSpace,
                   fill_type: str,
                   fill_params: FillParams,
                   mode: str = C.MODE_ENGRAVING) -> List[LayerBlock]:
    """Build and order the toolpaths for every enabled layer (back-end agnostic).

    Nearest-neighbour ordering carries the tool position across layers and passes,
    exactly as before. Plotter mode always runs a single pass (multi-pass only makes
    sense for deepening a laser burn)."""
    blocks: List[LayerBlock] = []
    cur: Point = (0.0, 0.0)
    for layer, setting in layer_settings:
        if not setting.enabled:
            continue
        burns = build_layer_toolpaths(layer, setting, machine, fill_type, fill_params)
        if not burns:
            continue
        passes = 1 if mode == C.MODE_PLOTTER else max(1, setting.passes)
        pls: List[Polyline] = []
        for _pass in range(passes):
            ordered, cur = order_polylines(burns, cur)
            pls.extend(ordered)
        blocks.append((getattr(layer, "label", "?"), setting, pls))
    return blocks


def emit_grbl(blocks: List[LayerBlock], machine: G.MachineSpace, options: GenOptions,
              log: Optional[LogFn] = None) -> GcodeProgram:
    """Emit a GRBL program from planned blocks.

    Engraving: M3/M4 S<power> on, M5 off, feed = layer speed (mm/min).
    Plotter:   servo pen DOWN (M3) / UP (M5) with a settle dwell, feed = speed*60."""

    def _log(actor: str, msg: str) -> None:
        if log:
            log(actor, msg)

    plotter = (options.mode == C.MODE_PLOTTER)
    prog = GcodeProgram()
    prog.line("; jasGrbl %s (%s)"
              % (options.version, C.MODE_LABELS.get(options.mode, options.mode)))
    if options.timestamp:
        prog.line("; generated %s" % options.timestamp)
    prog.line("; board %s x %s mm, home=%s"
              % (_fmt(machine.width_mm), _fmt(machine.height_mm), machine.home))
    prog.line("G21")   # mm
    prog.line("G90")   # absolute
    prog.line("G17")   # XY plane
    prog.line(C.PEN_UP_CMD if plotter else "M5")   # pen up / laser off
    prog.line("G0 X0 Y0")

    dwell = "G4 P%s" % _fmt(C.PEN_SETTLE_S)
    cur: Point = (0.0, 0.0)
    oob = 0

    for label, setting, pls in blocks:
        if plotter:
            feed = max(1, int(round(setting.plotter_speed * 60)))
            _log("INFO", "layer '%s': %d contours, force=%dg, speed=%dmm/s"
                 % (label, len(pls), setting.force, setting.plotter_speed))
            prog.line("; ---- layer '%s' force=%dg speed=%dmm/s ----"
                      % (label, setting.force, setting.plotter_speed))
        else:
            s_value = int(round(max(0, min(100, setting.power)) / 100.0 * options.s_max))
            feed = max(1, setting.speed)
            _log("INFO", "layer '%s': %d contours, power=%d%% (S%d), speed=%d, passes=%d"
                 % (label, len(pls), setting.power, s_value, setting.speed, setting.passes))
            prog.line("; ---- layer '%s' power=%d%% speed=%d passes=%d ----"
                      % (label, setting.power, setting.speed, setting.passes))
        prog.line("F%d" % feed)

        for pl in pls:
            start = pl[0]
            if not machine.in_bounds(*start):
                oob += 1
            # travel to start (pen up / laser off)
            prog.line("G0 X%s Y%s" % (_fmt(start[0]), _fmt(start[1])))
            prog.segments.append(("travel", cur, start))
            # pen down / laser on
            if plotter:
                prog.line(C.PEN_DOWN_CMD)
                prog.line(dwell)
            else:
                prog.line("%s S%d" % (options.laser_mode, s_value))
            # preview segments follow the (fine) polyline exactly
            prev = start
            for pt in pl[1:]:
                if not machine.in_bounds(*pt):
                    oob += 1
                prog.segments.append(("burn", prev, pt))
                prev = pt
            # G-code: emit real arcs (G2/G3) for curved runs, lines for straight runs
            _emit_burn_moves(prog, pl, options)
            # pen up / laser off at end of contour
            prog.line(C.PEN_UP_CMD)
            if plotter:
                prog.line(dwell)
            cur = pl[-1]

    prog.line(C.PEN_UP_CMD if plotter else "M5")
    if options.park_home:
        prog.line("G0 X0 Y0")
        prog.segments.append(("travel", cur, (0.0, 0.0)))
    prog.line("; jasGrbl end")

    if oob:
        _log("WARN", "%d toolpath point(s) fall outside the %sx%s mm work area"
             % (oob, _fmt(machine.width_mm), _fmt(machine.height_mm)))
    _log("INFO", "G-code ready: %d lines, burn %.1f mm, travel %.1f mm"
         % (len(prog.lines), prog.burn_length(), prog.travel_length()))
    return prog


def _hpgl_coord(p: Point, swap_xy: bool = False) -> str:
    x = round(p[0] * C.HPGL_UNITS_PER_MM)
    y = round(p[1] * C.HPGL_UNITS_PER_MM)
    return "%d,%d" % ((y, x) if swap_xy else (x, y))


def emit_hpgl(blocks: List[LayerBlock], machine: G.MachineSpace,
              options: GenOptions, log: Optional[LogFn] = None) -> str:
    """Emit an HPGL plot from the same planned blocks (for pen/knife plotters).

    Uses PU/PD (pen up/down) with per-layer VS (velocity, cm/s) and FS (force, g).
    Coordinates are in plotter units (0.025 mm) in the machine frame, so the plot
    matches the GRBL output and the preview."""
    swap = getattr(options, "swap_xy", False)
    out: List[str] = ["IN;", "SP1;"]
    for _label, setting, pls in blocks:
        vs = max(1, int(round(setting.plotter_speed / 10.0)))   # mm/s -> cm/s
        out.append("VS%d;" % vs)
        out.append("FS%d;" % max(1, setting.force))
        for pl in pls:
            if len(pl) < 2:
                continue
            out.append("PU%s;" % _hpgl_coord(pl[0], swap))
            out.append("PD%s;" % ",".join(_hpgl_coord(p, swap) for p in pl[1:]))
    out.append("PU;")
    out.append("SP0;")
    if log:
        log("INFO", "HPGL ready: %d layer block(s)" % len(blocks))
    return "\n".join(out) + "\n"


def generate_program(layer_settings: List[Tuple[object, LayerSetting]],
                     machine: G.MachineSpace,
                     options: GenOptions,
                     fill_type: str,
                     fill_params: FillParams,
                     log: Optional[LogFn] = None) -> GcodeProgram:
    """Build a full GRBL GcodeProgram from (LayerData, LayerSetting) pairs.

    Convenience wrapper (plan + emit_grbl); the UI plans once and emits both GRBL
    and HPGL from the shared blocks."""
    blocks = plan_toolpaths(layer_settings, machine, fill_type, fill_params, options.mode)
    return emit_grbl(blocks, machine, options, log)
