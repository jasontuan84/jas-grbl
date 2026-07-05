"""Fit a polyline with a mix of straight lines and circular arcs.

Used so the G-code emits real GRBL arcs (G2/G3) for curved toolpaths instead of a
long chain of tiny straight G1 moves. Greedy: at each point, extend the longest
line OR the longest arc within tolerance, whichever covers more points.

A "move" is one of:
    ("line", end)
    ("arc", end, center, cw, radius)
all coordinates in machine millimetres.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

Point = Tuple[float, float]


def _dedupe(pts: List[Point], eps: float = 1e-7) -> List[Point]:
    out: List[Point] = []
    for p in pts:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > eps:
            out.append(p)
    return out


def circle_through(a: Point, b: Point, c: Point) -> Optional[Tuple[Point, float]]:
    """Circumcircle of three points; None if (near) collinear."""
    ax, ay = a
    bx, by = b
    cx, cy = c
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    a2 = ax * ax + ay * ay
    b2 = bx * bx + by * by
    c2 = cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    center = (ux, uy)
    r = math.hypot(ax - ux, ay - uy)
    return center, r


def _max_dev_line(pts: List[Point], i: int, j: int) -> float:
    ax, ay = pts[i]
    bx, by = pts[j]
    dx, dy = bx - ax, by - ay
    seg = math.hypot(dx, dy)
    if seg < 1e-12:
        return max(math.hypot(pts[k][0] - ax, pts[k][1] - ay) for k in range(i, j + 1))
    worst = 0.0
    for k in range(i + 1, j):
        px, py = pts[k]
        dev = abs((px - ax) * dy - (py - ay) * dx) / seg
        if dev > worst:
            worst = dev
    return worst


def _arc_valid(pts, i, j, center, r, tol) -> bool:
    cx, cy = center
    for k in range(i, j + 1):
        if abs(math.hypot(pts[k][0] - cx, pts[k][1] - cy) - r) > tol:
            return False
    sign = 0
    total = 0.0
    for k in range(i, j):
        a0 = math.atan2(pts[k][1] - cy, pts[k][0] - cx)
        a1 = math.atan2(pts[k + 1][1] - cy, pts[k + 1][0] - cx)
        da = a1 - a0
        while da > math.pi:
            da -= 2 * math.pi
        while da < -math.pi:
            da += 2 * math.pi
        if da == 0:
            continue
        s = 1 if da > 0 else -1
        if sign == 0:
            sign = s
        elif s != sign:
            return False
        # The sample points lie ON the circle, but the *path between* two
        # consecutive points is the straight polyline segment (chord). The fitted
        # arc bulges away from that chord by the sagitta r*(1-cos(dtheta/2)); if
        # the points are far apart angularly this bulge is huge, so the arc would
        # replace a straight run with a big phantom loop. Reject when any single
        # step's bulge exceeds tolerance so the arc always tracks the polyline.
        if r * (1.0 - math.cos(abs(da) / 2.0)) > tol:
            return False
        total += da
    return abs(total) <= math.radians(350.0)


def _is_cw(p0: Point, p1: Point, center: Point) -> bool:
    cx, cy = center
    a0 = math.atan2(p0[1] - cy, p0[0] - cx)
    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    da = a1 - a0
    while da > math.pi:
        da -= 2 * math.pi
    while da < -math.pi:
        da += 2 * math.pi
    return da < 0


def arc_sweep(p0: Point, p1: Point, center: Point, cw: bool) -> float:
    """Swept angle (radians, >=0) from p0 to p1 around center in the given direction."""
    cx, cy = center
    a0 = math.atan2(p0[1] - cy, p0[0] - cx)
    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    if cw:
        return (a0 - a1) % (2 * math.pi)
    return (a1 - a0) % (2 * math.pi)


def arc_length(p0: Point, p1: Point, center: Point, cw: bool, r: float) -> float:
    return r * arc_sweep(p0, p1, center, cw)


def fit_arcs(points: List[Point], tol: float, max_radius: float = 8000.0) -> List[tuple]:
    pts = _dedupe(points)
    n = len(pts)
    moves: List[tuple] = []
    if n < 2:
        return moves
    i = 0
    while i < n - 1:
        # longest straight run from i
        jl = i + 1
        while jl + 1 < n and _max_dev_line(pts, i, jl + 1) <= tol:
            jl += 1
        # longest arc run from i (needs >= 3 points)
        ja = i
        best = None
        if n - i >= 3:
            j = i + 2
            while j < n:
                tri = circle_through(pts[i], pts[(i + j) // 2], pts[j])
                if tri is None:
                    break
                center, r = tri
                if r > max_radius or not _arc_valid(pts, i, j, center, r, tol):
                    break
                ja, best = j, (center, r)
                j += 1
        if best is not None and ja > jl:
            center, r = best
            cw = _is_cw(pts[i], pts[i + 1], center)
            moves.append(("arc", pts[ja], center, cw, r))
            i = ja
        else:
            moves.append(("line", pts[jl]))
            i = jl
    return moves
