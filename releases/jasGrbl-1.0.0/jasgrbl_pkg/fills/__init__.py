"""Fill strategy registry.

Each strategy maps a filled region (list of rings in machine mm) to an ordered
list of burn polylines. The registry is the single source the UI reads, so adding
a strategy is one import + one dict entry.
"""

from __future__ import annotations

import math
from typing import List, Tuple

from .. import constants as C
from .. import geometry as G
from .base import FillParams, FillStrategy
from .hatch import CrossHatchFill, HatchFill, ZigzagFill
from .spacefill import HilbertFill, PeanoFill
from .offsetfill import ContourFill, SpiralFill
from .voronoi import VoronoiFill

FILL_REGISTRY = {
    C.FILL_HATCH: HatchFill(),
    C.FILL_CROSSHATCH: CrossHatchFill(),
    C.FILL_ZIGZAG: ZigzagFill(),
    C.FILL_CONTOUR: ContourFill(),
    C.FILL_SPIRAL: SpiralFill(),
    C.FILL_HILBERT: HilbertFill(),
    C.FILL_PEANO: PeanoFill(),
    C.FILL_VORONOI: VoronoiFill(),
}


def get_fill(fill_type: str) -> FillStrategy:
    return FILL_REGISTRY.get(fill_type, FILL_REGISTRY[C.FILL_ZIGZAG])


def auto_select(rings: List[G.Polyline], spacing: float) -> Tuple[str, float]:
    """Auto fill: pick the lift-optimal connected fill for one region + its scan angle.
    Returns (fill_type, angle_deg).

    Cost model (doc 03 s1): for a fixed area+spacing the pen-down length is ~Area/spacing
    for any line fill, so engaged distance does NOT distinguish strategies - what dominates
    machine time is the number of tool LIFTS (disengage->travel->re-engage). Treating the
    laser like a pen plotter, the goal is to minimise lifts. Auto only ever returns a
    connected fill (Zigzag / Spiral / Contour); the disjoint/decorative fills
    (Hatch, Cross-Hatch, Hilbert, Peano, Voronoi) are manual overrides only.

    Because the Spiral engine now connects offset rings across ANY topology (holes,
    concavity, multiple nests -> one stroke per nest, see fills.offsetfill), it is the
    lift-optimal choice for essentially every real filled region and is preferred broadly.
    This deliberately strengthens the knowledge-base rule set (doc 03 s4), which routed
    holed/concave shapes to Contour only because it assumed a convex-only Spiral. Zigzag is
    kept for rectangles / straight bars (boustrophedon along the long axis is the natural,
    longest-run, ~0-lift fill there) and Contour for hairline regions (no interior to spiral).

    The shape is classified from its net area, min-area oriented bbox (principal axis), mean
    stroke width (in pen-lines), rectangularity, convexity, compactness and hole count.
    """
    valid = [r for r in rings if len(r) >= 3]
    if not valid:
        return (C.FILL_ZIGZAG, 0.0)

    # Per-subpath absolute area + total perimeter; collect all points for the hull.
    abs_areas = [abs(G.polygon_area(r)) for r in valid]
    perim = sum(G.polyline_length(r) for r in valid)
    all_pts = [p for r in valid for p in r]

    # Even-odd nesting depth (a boundary vertex inside an odd number of larger subpaths is a
    # hole) -> net area + hole count. Winding/centroid independent (handles concentric rings).
    n = len(valid)
    net = 0.0
    holes = 0
    for i in range(n):
        depth = sum(1 for j in range(n)
                    if j != i and abs_areas[j] > abs_areas[i]
                    and G.point_in_rings(valid[i][0], [valid[j]]))
        if depth % 2 == 0:
            net += abs_areas[i]
        else:
            net -= abs_areas[i]
            holes += 1
    area = max(0.0, net)

    hull = G.convex_hull(all_pts)
    hull_area = abs(G.polygon_area(hull)) if len(hull) >= 3 else 0.0
    L, Wd, theta = G.min_area_obb(hull)

    elong = (L / Wd) if Wd > 1e-9 else 1.0
    rect = (area / (L * Wd)) if (L * Wd) > 1e-9 else 0.0
    convex = min(1.0, area / hull_area) if hull_area > 1e-9 else 1.0
    compact = (4.0 * math.pi * area) / (perim * perim) if perim > 1e-9 else 0.0
    w_mean = (2.0 * area / perim) if perim > 1e-9 else 0.0
    w_lines = (w_mean / spacing) if spacing > 1e-9 else 0.0

    # First match wins. Ordered from "strategy irrelevant" to the lift-optimal default.
    if area < 4.0 * spacing * spacing:
        return (C.FILL_ZIGZAG, theta)                       # tiny: 1-2 passes, any fill ~ same
    if w_lines < 1.8:
        return (C.FILL_CONTOUR, theta)                      # hairline: no interior, hug the outline
    if w_lines < 6.0 and elong > 3.5:                       # thin elongated stroke
        return (C.FILL_ZIGZAG if rect > 0.88 else C.FILL_SPIRAL, theta)  # straight bar vs curved
    if rect > 0.82 and convex > 0.88 and holes == 0:
        return (C.FILL_ZIGZAG, theta)                       # square/rectangle: boustrophedon
    return (C.FILL_SPIRAL, theta)                           # blobs, concave, holed, text: fewest lifts


__all__ = ["FILL_REGISTRY", "get_fill", "auto_select", "FillParams", "FillStrategy"]
