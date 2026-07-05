"""Unit tests for the pure-Python core (no inkex / no GTK required).

Run directly:  python tests/test_core.py
Or with pytest: pytest tests/
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jasgrbl_pkg import constants as C  # noqa: E402
from jasgrbl_pkg import geometry as G  # noqa: E402
from jasgrbl_pkg.config import LayerSetting  # noqa: E402
from jasgrbl_pkg.fills import get_fill  # noqa: E402
from jasgrbl_pkg.fills.base import FillParams  # noqa: E402
from jasgrbl_pkg.gcode import (  # noqa: E402
    ORDER_NN_CAP, GenOptions, build_layer_toolpaths, generate_program, order_polylines)


SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]


def _approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


# --------------------------------------------------------------- geometry
def test_machine_mapping_roundtrip():
    for home in C.HOME_POSITIONS:
        ms = G.MachineSpace(400, 300, home)
        for (x, y) in [(0, 0), (100, 50), (400, 300)]:
            mx, my = ms.to_machine(x, y)
            sx, sy = ms.to_svg_mm(mx, my)
            assert _approx(sx, x) and _approx(sy, y), (home, x, y, sx, sy)


def test_machine_keeps_document_position():
    # The design is mapped as-is (no re-arrangement): a square far from the origin in
    # the document stays far from Home. With bottom-left home the nearest-Home corner
    # of a 100..110 square lands at (100, height-110), not moved to the origin.
    ms = G.MachineSpace(400, 300, C.HOME_BOTTOM_LEFT)
    sq = [(100.0, 100.0), (110.0, 100.0), (110.0, 110.0), (100.0, 110.0)]
    pts = [ms.to_machine(x, y) for x, y in sq]
    assert _approx(min(p[0] for p in pts), 100.0)
    assert _approx(min(p[1] for p in pts), 300.0 - 110.0)


def test_machine_mapping_corners():
    ms = G.MachineSpace(400, 300, C.HOME_BOTTOM_LEFT)
    assert ms.to_machine(0, 0) == (0, 300)       # SVG top-left -> machine top
    assert ms.to_machine(0, 300) == (0, 0)       # SVG bottom-left -> machine origin
    ms2 = G.MachineSpace(400, 300, C.HOME_TOP_LEFT)
    assert ms2.to_machine(0, 0) == (0, 0)


def test_point_in_rings():
    assert G.point_in_rings((5, 5), [SQUARE]) is True
    assert G.point_in_rings((15, 5), [SQUARE]) is False
    # square with a hole 4..6
    hole = [(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]
    assert G.point_in_rings((5, 5), [SQUARE, hole]) is False
    assert G.point_in_rings((2, 2), [SQUARE, hole]) is True


def test_polygon_area():
    assert _approx(G.polygon_area(SQUARE), 100.0)            # CCW positive
    assert _approx(G.polygon_area(list(reversed(SQUARE))), -100.0)


def test_flatten_cubic_straight_line():
    out = []
    G.flatten_cubic((0, 0), (1, 0), (2, 0), (3, 0), 0.1, out)
    assert out[-1] == (3, 0)
    for (x, y) in out:
        assert _approx(y, 0.0, 1e-9)


def test_flatten_cubic_curve_endpoint():
    out = []
    G.flatten_cubic((0, 0), (0, 10), (10, 10), (10, 0), 0.05, out)
    assert _approx(out[-1][0], 10.0) and _approx(out[-1][1], 0.0)
    assert len(out) > 4  # actually subdivided


# ------------------------------------------------------------------ fills
def test_hatch_fill_square():
    strat = get_fill(C.FILL_HATCH)
    segs = strat.generate([SQUARE], FillParams(spacing=1.0, angle=0.0))
    assert len(segs) >= 8
    for seg in segs:
        assert len(seg) == 2  # independent segments


def test_zigzag_is_connected():
    strat = get_fill(C.FILL_ZIGZAG)
    polys = strat.generate([SQUARE], FillParams(spacing=1.0, angle=0.0))
    # zigzag should produce far fewer, longer polylines than hatch
    longest = max(len(p) for p in polys)
    assert longest > 2


def test_crosshatch_denser_than_hatch():
    h = get_fill(C.FILL_HATCH).generate([SQUARE], FillParams(spacing=1.0, angle=0.0))
    x = get_fill(C.FILL_CROSSHATCH).generate([SQUARE], FillParams(spacing=1.0, angle=0.0))
    assert len(x) > len(h)


def test_hilbert_inside_region():
    polys = get_fill(C.FILL_HILBERT).generate([SQUARE], FillParams(spacing=1.0))
    assert polys
    for p in polys:
        for (x, y) in p:
            assert -0.5 <= x <= 10.5 and -0.5 <= y <= 10.5


def test_peano_produces_paths():
    polys = get_fill(C.FILL_PEANO).generate([SQUARE], FillParams(spacing=2.0))
    assert polys


def test_contour_nested_rings():
    polys = get_fill(C.FILL_CONTOUR).generate([SQUARE], FillParams(spacing=1.0))
    assert len(polys) >= 2  # at least outer + one inset


def test_voronoi_fallback_no_scipy():
    # Should not raise even when scipy is missing (falls back to hatch).
    polys = get_fill(C.FILL_VORONOI).generate([SQUARE], FillParams(spacing=1.0))
    assert polys is not None


# ------------------------------------------------------------- auto fill
def _circle_ring(cx, cy, r, n=64):
    return [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n))
            for k in range(n + 1)]


def test_auto_straight_bar_picks_zigzag():
    # thin elongated + rectangular (rect>0.88) -> zigzag along the long axis
    from jasgrbl_pkg.fills import auto_select
    bar = [(0.0, 0.0), (40.0, 0.0), (40.0, 3.0), (0.0, 3.0), (0.0, 0.0)]
    name, angle = auto_select([bar], 0.5)
    assert name == C.FILL_ZIGZAG
    assert abs(angle) < 1.0 or abs(abs(angle) - 180.0) < 1.0   # long axis ~horizontal


def test_auto_round_blob_picks_spiral():
    # compact, convex, no holes, rect<0.82 -> spiral
    from jasgrbl_pkg.fills import auto_select
    name, _ = auto_select([_circle_ring(0, 0, 20)], 0.5)
    assert name == C.FILL_SPIRAL


def test_auto_rectangle_picks_zigzag():
    # big open rectangle (rect~1) -> zigzag
    from jasgrbl_pkg.fills import auto_select
    sq = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0), (0.0, 0.0)]
    name, _ = auto_select([sq], 0.5)
    assert name == C.FILL_ZIGZAG


def test_auto_with_hole_picks_spiral():
    # a region WITH a hole -> spiral (connects the offset rings into few strokes, so
    # far fewer lifts than drawing each contour ring separately)
    from jasgrbl_pkg.fills import auto_select
    outer = [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0), (0.0, 0.0)]
    hole = [(15.0, 15.0), (25.0, 15.0), (25.0, 25.0), (15.0, 25.0), (15.0, 15.0)]
    name, _ = auto_select([outer, hole], 0.5)
    assert name == C.FILL_SPIRAL


def test_auto_concave_picks_spiral():
    # an L-bracket (strong concavity, no hole) -> spiral, not zigzag/contour
    from jasgrbl_pkg.fills import auto_select
    ell = [(0.0, 0.0), (30.0, 0.0), (30.0, 10.0), (10.0, 10.0),
           (10.0, 30.0), (0.0, 30.0), (0.0, 0.0)]
    name, _ = auto_select([ell], 0.5)
    assert name == C.FILL_SPIRAL


def test_spiral_fewer_lifts_than_contour_on_hole():
    # the whole point: Spiral must lift far less often than Contour on a holed shape.
    outer = [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0), (0.0, 0.0)]
    hole = [(15.0, 15.0), (25.0, 15.0), (25.0, 25.0), (15.0, 25.0), (15.0, 15.0)]
    p = FillParams(spacing=1.0, angle=0.0)
    contour = get_fill(C.FILL_CONTOUR).generate([outer, hole], p)
    spiral = get_fill(C.FILL_SPIRAL).generate([outer, hole], p)
    assert len(spiral) < len(contour), (len(spiral), len(contour))


def test_auto_gcode_runs():
    layer = _square_layer(has_fill=True, has_stroke=False)
    ms = G.MachineSpace(400, 400, C.HOME_BOTTOM_LEFT)
    prog = generate_program([(layer, LayerSetting())], ms, GenOptions(version="t"),
                           C.FILL_AUTO, FillParams(spacing=1.0))
    assert prog.burn_length() > 0


# ------------------------------------------------------------------ arcs
def test_fit_arcs_straight_line():
    from jasgrbl_pkg.arcs import fit_arcs
    pts = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0), (4.0, 0.0)]
    moves = fit_arcs(pts, 0.05)
    assert len(moves) == 1 and moves[0][0] == "line"
    assert moves[0][1] == (4.0, 0.0)


def test_fit_arcs_circle():
    from jasgrbl_pkg.arcs import fit_arcs, arc_length
    pts = [(10.0 * math.cos(math.radians(d)), 10.0 * math.sin(math.radians(d)))
           for d in range(0, 271, 3)]
    moves = fit_arcs(pts, 0.05)
    arcs = [m for m in moves if m[0] == "arc"]
    assert arcs, "a sampled circle should fit at least one arc"
    # endpoint exactness (GRBL needs start/end on the circle)
    assert _approx(moves[-1][1][0], pts[-1][0], 1e-6)
    assert _approx(moves[-1][1][1], pts[-1][1], 1e-6)
    # reconstructed arc length ~ 270deg of r=10  => 2*pi*10*0.75 ~= 47.1 mm
    total = 0.0
    prev = pts[0]
    for m in moves:
        if m[0] == "arc":
            _, end, center, cw, r = m
            total += arc_length(prev, end, center, cw, r)
        else:
            total += G._dist(prev, m[1])
        prev = m[1]
    assert 45.0 < total < 49.0, "arc length should match ~47.1mm, got %.2f" % total


# ------------------------------------------------------------ centerline
def test_centerline_thick_bar():
    from jasgrbl_pkg.centerline import centerline
    bar = [(0.0, 0.0), (20.0, 0.0), (20.0, 2.0), (0.0, 2.0), (0.0, 0.0)]
    strokes = centerline([bar])
    assert strokes, "centerline should produce at least one stroke"
    total = sum(G.polyline_length(s) for s in strokes)
    assert total > 12.0, "centerline of a 20mm bar should be a long single stroke"
    ys = [p[1] for s in strokes for p in s]
    mid = sum(ys) / len(ys)
    assert 0.4 < mid < 1.6, "centerline should run down the middle (y~1.0), got %.2f" % mid


def test_centerline_shorter_than_fill():
    from jasgrbl_pkg.centerline import centerline
    bar = [(0.0, 0.0), (20.0, 0.0), (20.0, 2.0), (0.0, 2.0), (0.0, 0.0)]
    cl = sum(G.polyline_length(s) for s in centerline([bar]))
    fill = sum(G.polyline_length(s) for s in
               get_fill(C.FILL_HATCH).generate([bar], FillParams(spacing=0.3, angle=0)))
    assert cl < fill, "single-stroke centerline must be shorter than a hatch fill"


# ------------------------------------------------------------------ gcode
def _square_layer(has_fill=False, has_stroke=True):
    shape = G.ShapeData(
        element_id="sq", subpaths=[SQUARE], closed=[True],
        has_fill=has_fill, has_stroke=has_stroke)
    return G.LayerData(layer_id="L1", label="Cut", visible=True, shapes=[shape])


def test_gcode_stroke_square():
    layer = _square_layer(has_stroke=True)
    ms = G.MachineSpace(400, 400, C.HOME_BOTTOM_LEFT)
    prog = generate_program(
        [(layer, LayerSetting(power=50, speed=1000, passes=1))],
        ms, GenOptions(s_max=1000, version="t"), C.FILL_HATCH, FillParams())
    text = prog.text()
    assert "G21" in text and "G90" in text
    assert "M4 S500" in text          # 50% of 1000
    assert "M5" in text
    assert text.strip().endswith("; jasGrbl end")
    # perimeter of a 10mm square == 40mm of burn
    assert _approx(prog.burn_length(), 40.0, 1e-3)


def test_gcode_passes_repeat():
    layer = _square_layer(has_stroke=True)
    ms = G.MachineSpace(400, 400, C.HOME_BOTTOM_LEFT)
    one = generate_program([(layer, LayerSetting(passes=1))], ms,
                           GenOptions(version="t"), C.FILL_HATCH, FillParams())
    two = generate_program([(layer, LayerSetting(passes=2))], ms,
                           GenOptions(version="t"), C.FILL_HATCH, FillParams())
    assert _approx(two.burn_length(), 2 * one.burn_length(), 1e-3)


def test_gcode_disabled_layer_skipped():
    layer = _square_layer()
    ms = G.MachineSpace(400, 400, C.HOME_BOTTOM_LEFT)
    prog = generate_program([(layer, LayerSetting(enabled=False))], ms,
                           GenOptions(version="t"), C.FILL_HATCH, FillParams())
    assert _approx(prog.burn_length(), 0.0)


def test_gcode_fill_square_has_burn():
    layer = _square_layer(has_fill=True, has_stroke=False)
    ms = G.MachineSpace(400, 400, C.HOME_BOTTOM_LEFT)
    prog = generate_program([(layer, LayerSetting())], ms, GenOptions(version="t"),
                           C.FILL_HATCH, FillParams(spacing=1.0, angle=0.0))
    assert prog.burn_length() > 0


# -------------------------------------------------------------- ordering
def test_order_reroots_closed_ring_to_nearest_vertex():
    # A closed ring may be entered at any vertex; ordering from near the (10,10)
    # corner must re-root the square there, not always start at its stored (0,0).
    ordered, _cur = order_polylines([SQUARE], start=(9.0, 11.0))
    assert ordered[0][0] == (10.0, 10.0), ordered[0][0]
    assert ordered[0][0] == ordered[0][-1]                 # still closed
    assert _approx(G.polyline_length(ordered[0]), 40.0)    # full perimeter preserved


def test_order_reverses_open_chain():
    open_chain = [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0)]
    ordered, cur = order_polylines([open_chain], start=(11.0, 0.0))
    assert ordered[0][0] == (10.0, 0.0)                    # entered from the near end
    assert _approx(cur[0], 0.0)


def test_order_dense_falls_back_but_keeps_all_chains():
    chains = [[(float(i), 0.0), (float(i), 1.0)] for i in range(ORDER_NN_CAP + 50)]
    ordered, cur = order_polylines(chains, start=(0.0, 0.0))
    assert len(ordered) == len(chains)                     # nothing dropped
    assert math.isfinite(cur[0]) and math.isfinite(cur[1])


# --------------------------------------------------- zigzag / hole bridging
def test_zigzag_does_not_bridge_hole():
    outer = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0), (0.0, 0.0)]
    hole = [(8.0, 8.0), (12.0, 8.0), (12.0, 12.0), (8.0, 12.0), (8.0, 8.0)]
    polys = get_fill(C.FILL_ZIGZAG).generate([outer, hole], FillParams(spacing=1.0, angle=0.0))
    for pl in polys:
        for i in range(len(pl) - 1):
            mx = (pl[i][0] + pl[i + 1][0]) / 2.0
            my = (pl[i][1] + pl[i + 1][1]) / 2.0
            assert not G.point_in_rings((mx, my), [hole]), \
                "a burn/connector segment crosses the hole at (%.2f,%.2f)" % (mx, my)


# ------------------------------------------------------- polyline hygiene
def test_clean_polyline_drops_nan_and_dupes():
    pts = [(0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (float("nan"), 2.0),
           (float("inf"), 3.0), (2.0, 2.0)]
    out = G.clean_polyline(pts)
    assert out == [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)]


# ------------------------------------------------------ shade-by-colour
def test_fill_luminance_ordering():
    lum = G._fill_luminance
    assert _approx(lum("#000000"), 0.0, 1e-6)
    assert _approx(lum("#ffffff"), 1.0, 1e-6)
    assert _approx(lum(None), 0.0)                     # absent fill = SVG default black
    assert lum("#000000") < lum("#808080") < lum("#ffffff")
    assert lum("#333333") < lum("#cccccc")             # dark grey darker than light grey
    assert _approx(lum("rgb(0,0,0)"), 0.0)
    assert lum("red") < lum("yellow")                  # perceptual weighting


def test_shade_density_darker_is_denser():
    ms = G.MachineSpace(400, 400, C.HOME_BOTTOM_LEFT)
    big = [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0), (0.0, 0.0)]

    def fill_lines(gray):
        shape = G.ShapeData("s", [big], [True], has_fill=True, fill_gray=gray)
        layer = G.LayerData("L", "L", True, [shape])
        return build_layer_toolpaths(
            layer, LayerSetting(), ms, C.FILL_HATCH,
            FillParams(spacing=0.5, angle=0.0, shade_density=True))

    dark = fill_lines(0.0)      # black -> base spacing (densest)
    light = fill_lines(0.85)    # light grey -> much wider spacing (sparsest)
    assert len(dark) > len(light) * 2, (len(dark), len(light))


def test_shade_density_off_ignores_colour():
    ms = G.MachineSpace(400, 400, C.HOME_BOTTOM_LEFT)
    big = [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0), (0.0, 0.0)]

    def fill_lines(gray):
        shape = G.ShapeData("s", [big], [True], has_fill=True, fill_gray=gray)
        layer = G.LayerData("L", "L", True, [shape])
        return build_layer_toolpaths(
            layer, LayerSetting(), ms, C.FILL_HATCH,
            FillParams(spacing=0.5, angle=0.0, shade_density=False))

    assert len(fill_lines(0.0)) == len(fill_lines(0.85))   # colour ignored when off


# ------------------------------------------------------------------ runner
def _run_all():
    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for fn in funcs:
        try:
            fn()
            passed += 1
            print("PASS  %s" % fn.__name__)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL  %s: %s" % (fn.__name__, exc))
    print("\n%d passed, %d failed" % (passed, failed))
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
