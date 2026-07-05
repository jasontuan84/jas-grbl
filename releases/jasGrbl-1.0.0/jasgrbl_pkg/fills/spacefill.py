"""Space-filling-curve fills: Hilbert and Peano, clipped to the region."""

from __future__ import annotations

import math
from typing import Dict, List

from .. import geometry as G
from .base import FillParams, FillStrategy, Polyline, Ring


def _lsystem(axiom: str, rules: Dict[str, str], iterations: int) -> str:
    s = axiom
    for _ in range(iterations):
        s = "".join(rules.get(ch, ch) for ch in s)
    return s


def _turtle(commands: str, angle_deg: float = 90.0) -> List[G.Point]:
    """Interpret an L-system string into integer-grid points. Headings are
    multiples of 90 deg so coordinates stay on an integer lattice (rounded)."""
    x = y = 0.0
    heading = 0.0
    pts: List[G.Point] = [(0, 0)]
    for ch in commands:
        if ch == "F":
            x += math.cos(math.radians(heading))
            y += math.sin(math.radians(heading))
            pts.append((round(x), round(y)))
        elif ch == "+":
            heading += angle_deg
        elif ch == "-":
            heading -= angle_deg
    return pts


def _clip_curve_to_region(points: List[G.Point], rings: List[Ring]) -> List[Polyline]:
    """Keep curve segments whose midpoint lies inside the region; break elsewhere."""
    out: List[Polyline] = []
    current: Polyline = []
    for i in range(len(points) - 1):
        a = points[i]
        b = points[i + 1]
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        if G.point_in_rings(mid, rings):
            if not current:
                current = [a]
            current.append(b)
        else:
            if len(current) >= 2:
                out.append(current)
            current = []
    if len(current) >= 2:
        out.append(current)
    return out


class _CurveFill(FillStrategy):
    axiom = "A"
    rules: Dict[str, str] = {}
    base = 2          # 2 for Hilbert, 3 for Peano
    max_order = 8

    def generate(self, rings: List[Ring], params: FillParams) -> List[Polyline]:
        closed = [G.close_ring(r) for r in rings if len(r) >= 2]
        if not closed:
            return []
        xmin, ymin, xmax, ymax = G.rings_bbox(closed)
        maxdim = max(xmax - xmin, ymax - ymin)
        spacing = params.spacing if params.spacing > 0 else 0.3
        cells_needed = max(2, math.ceil(maxdim / spacing))
        order = max(1, math.ceil(math.log(cells_needed) / math.log(self.base)))
        if order > self.max_order:
            order = self.max_order
            params.warn(
                "%s fill capped at order %d; effective spacing is coarser than requested"
                % (self.name, order)
            )
        extent = (self.base ** order) - 1
        if extent <= 0:
            return []
        step = maxdim / extent
        curve = _turtle(_lsystem(self.axiom, self.rules, order))
        scaled = [(xmin + px * step, ymin + py * step) for (px, py) in curve]
        return _clip_curve_to_region(scaled, closed)


class HilbertFill(_CurveFill):
    name = "hilbert"
    base = 2
    max_order = 8
    axiom = "A"
    rules = {
        "A": "+BF-AFA-FB+",
        "B": "-AF+BFB+FA-",
    }


class PeanoFill(_CurveFill):
    name = "peano"
    base = 3
    max_order = 5
    axiom = "L"
    rules = {
        "L": "LFRFL-F-RFLFR+F+LFRFL",
        "R": "RFLFR+F+LFRFL-F-RFLFR",
    }
