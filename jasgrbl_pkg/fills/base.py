"""Shared fill infrastructure: parameters, the scanline engine, and the ABC."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from .. import geometry as G

Polyline = G.Polyline
Ring = G.Polyline


@dataclass
class FillParams:
    spacing: float = 0.30   # mm between fill lines / curve cells (for the darkest fill)
    angle: float = 45.0     # degrees (hatch direction)
    log: Optional[Callable[[str, str], None]] = None
    shade_density: bool = True   # scale spacing by fill-colour luminance (doc 03 s2)

    def warn(self, message: str) -> None:
        if self.log:
            self.log("WARN", message)

    def info(self, message: str) -> None:
        if self.log:
            self.log("INFO", message)


class FillStrategy:
    """Base class. Subclasses implement generate()."""

    name = "base"

    def generate(self, rings: List[Ring], params: FillParams) -> List[Polyline]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Scanline engine (shared by Hatch / Cross-Hatch / Zigzag)
# ---------------------------------------------------------------------------


def _scanlines(rings: List[Ring], angle: float, spacing: float):
    """Return (lines, cx, cy, rot) where lines is a list of (y, [ (xa, xb), ... ])
    in the rotated frame; spans are inside-region intervals via the even-odd rule.
    ``rot`` is the region rings in that same rotated frame (used by the zigzag
    connector's inside-test)."""
    closed = [G.close_ring(r) for r in rings if len(r) >= 2]
    if not closed:
        return [], 0.0, 0.0, []
    xmin, ymin, xmax, ymax = G.rings_bbox(closed)
    cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
    rot = [G.rotate(r, -angle, cx, cy) for r in closed]
    rxmin, rymin, rxmax, rymax = G.rings_bbox(rot)

    lines = []
    if spacing <= 0:
        spacing = 0.3
    y = rymin + spacing / 2.0
    while y < rymax:
        xs: List[float] = []
        for ring in rot:
            for i in range(len(ring) - 1):
                x1, y1 = ring[i]
                x2, y2 = ring[i + 1]
                if (y1 <= y < y2) or (y2 <= y < y1):
                    t = (y - y1) / (y2 - y1)
                    xs.append(x1 + t * (x2 - x1))
        xs.sort()
        spans: List[Tuple[float, float]] = []
        k = 0
        while k + 1 < len(xs):
            spans.append((xs[k], xs[k + 1]))
            k += 2
        if spans:
            lines.append((y, spans))
        y += spacing
    return lines, cx, cy, rot


def hatch_segments(rings: List[Ring], angle: float, spacing: float) -> List[Polyline]:
    """Independent parallel segments (lift between each)."""
    lines, cx, cy, _rot = _scanlines(rings, angle, spacing)
    segs: List[Polyline] = []
    for y, spans in lines:
        for xa, xb in spans:
            segs.append([(xa, y), (xb, y)])
    return [G.rotate(seg, angle, cx, cy) for seg in segs]


def _connector_inside(a: Point, b: Point, rot_rings: List[Ring], center: Point) -> bool:
    """True if the connector a->b between two scan-line ends stays inside the region.

    Tests the connector midpoint, nudged a hair toward the region centre so a
    connector that merely rides the outer boundary (midpoint exactly on an edge,
    the normal convex case) counts as inside, while one that spans a hole - whose
    midpoint sits deep in the void - is correctly rejected (doc 03 s3.3)."""
    mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    dx, dy = center[0] - mid[0], center[1] - mid[1]
    d = (dx * dx + dy * dy) ** 0.5
    if d > 1e-12:
        eps = 1e-3
        mid = (mid[0] + dx / d * eps, mid[1] + dy / d * eps)
    return G.point_in_rings(mid, rot_rings)


def zigzag_polylines(rings: List[Ring], angle: float, spacing: float) -> List[Polyline]:
    """Boustrophedon: connect adjacent scan lines into continuous polylines, lifting
    when the next span is too far OR when the connecting move would leave the region
    (e.g. bridge a hole) - see doc 03 s3.3."""
    lines, cx, cy, rot = _scanlines(rings, angle, spacing)
    if rot:
        rxmin, rymin, rxmax, rymax = G.rings_bbox(rot)
        center = ((rxmin + rxmax) / 2.0, (rymin + rymax) / 2.0)
    else:
        center = (0.0, 0.0)
    polylines: List[Polyline] = []
    current: Optional[Polyline] = None
    flip = False
    join_tol = 2.0 * spacing
    for y, spans in lines:
        if flip:
            ordered = [(b, a) for (a, b) in reversed(spans)]
        else:
            ordered = list(spans)
        flip = not flip
        for xa, xb in ordered:
            start = (xa, y)
            end = (xb, y)
            if (current is not None and G._dist(current[-1], start) <= join_tol
                    and _connector_inside(current[-1], start, rot, center)):
                current.append(start)
                current.append(end)
            else:
                if current is not None:
                    polylines.append(current)
                current = [start, end]
    if current is not None:
        polylines.append(current)
    return [G.rotate(pl, angle, cx, cy) for pl in polylines]
