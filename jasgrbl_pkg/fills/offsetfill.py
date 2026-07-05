"""Contour and Spiral fills via inward offsetting.

Primary engine is a robust distance-field offset (``offset_dt``) that handles
holes, concavity and multiple disjoint regions - the cases where a naive
per-vertex inset self-intersects or gives up. The old miter-bisector inset is
kept only as a fallback for the rare case the distance-field engine declines
(e.g. a region too thin for even one ring); if both decline the caller drops to
the scanline Zigzag, so the extension never emits garbage silently.

Contour draws each offset ring (a few lifts, one per ring). Spiral connects the
rings into as few continuous strokes as possible - one per nested region - so the
tool lifts only when it must jump to a separate nest (doc 03 s3.4-3.5). Treating
the laser like a pen plotter, minimising lifts is the whole point.
"""

from __future__ import annotations

import math
from typing import List, Optional

from .. import geometry as G
from .base import FillParams, FillStrategy, Polyline, Ring


def _unit_inward_normals(pts: List[G.Point], sign: int):
    normals = []
    n = len(pts)
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]
        ex, ey = bx - ax, by - ay
        length = math.hypot(ex, ey)
        if length < 1e-12:
            normals.append((0.0, 0.0))
            continue
        # left normal (rotate edge +90), oriented inward by polygon winding sign
        normals.append((sign * (-ey / length), sign * (ex / length)))
    return normals


def _clean_ring(pts: List[G.Point], tol: float) -> List[G.Point]:
    """Drop consecutive points closer than tol (removes the tiny degenerate edges that
    repeated insetting introduces on coarse polygons)."""
    out: List[G.Point] = []
    for p in pts:
        if not out or G._dist(out[-1], p) > tol:
            out.append(p)
    while len(out) >= 2 and G._dist(out[0], out[-1]) <= tol:
        out.pop()
    return out


def _inset_once(ring: Ring, d: float) -> Optional[Ring]:
    """Inset a simple closed ring inward by distance d. Returns None if degenerate.

    Sharp / near-reflex vertices are beveled (offset along the averaged edge normal)
    instead of aborting, so a coarse polygon can be inset many times in a row."""
    pts = ring[:-1] if (len(ring) >= 2 and G._dist(ring[0], ring[-1]) < 1e-9) else list(ring)
    pts = _clean_ring(pts, d * 0.05)
    n = len(pts)
    if n < 3:
        return None
    area = G.polygon_area(pts)
    if abs(area) < 1e-9:
        return None
    sign = 1 if area > 0 else -1
    edge_normals = _unit_inward_normals(pts, sign)
    out: List[G.Point] = []
    for i in range(n):
        na = edge_normals[(i - 1) % n]
        nb = edge_normals[i]
        denom = 1.0 + (na[0] * nb[0] + na[1] * nb[1])
        if denom < 0.05:
            # sharp/near-reflex corner: offset along the averaged unit normal (bevel)
            sx, sy = na[0] + nb[0], na[1] + nb[1]
            ln = math.hypot(sx, sy)
            mx, my = (sx / ln, sy / ln) if ln > 1e-9 else (nb[0], nb[1])
        else:
            mx = (na[0] + nb[0]) / denom
            my = (na[1] + nb[1]) / denom
            mlen = math.hypot(mx, my)
            if mlen > 3.0:
                scale = 3.0 / mlen
                mx, my = mx * scale, my * scale
        out.append((pts[i][0] + d * mx, pts[i][1] + d * my))
    out = _clean_ring(out, d * 0.2)
    if len(out) < 3:
        return None
    new_area = G.polygon_area(out)
    # orientation must be preserved and area must shrink toward zero
    if new_area == 0 or (new_area > 0) != (area > 0):
        return None
    if abs(new_area) >= abs(area):
        return None
    return G.close_ring(out)


def _miter_contour_rings(rings: List[Ring], spacing: float) -> Optional[List[Ring]]:
    """Fallback: repeated miter inset. Only handles ONE simple ring; returns None
    for holes / multiple regions so the caller can drop to the scanline fill."""
    valid = [r for r in rings if len(r) >= 3]
    if len(valid) != 1:
        return None
    outer = G.close_ring(valid[0])
    result = [outer]
    current = outer
    max_rings = 5000
    while len(result) < max_rings:
        nxt = _inset_once(current, spacing)
        if nxt is None:
            break
        if G.polyline_length(nxt) < spacing * 2.0:
            break
        result.append(nxt)
        current = nxt
    if len(result) <= 1:
        return None
    return result


def _contour_rings(rings: List[Ring], spacing: float, params: FillParams) -> Optional[List[Ring]]:
    """Concentric inward-offset rings for an even-odd region, any topology.

    Robust distance-field engine first (holes / concavity / multiple nests all
    handled); miter inset only if that declines. None -> caller falls back."""
    valid = [r for r in rings if len(r) >= 3]
    if not valid:
        return None
    try:
        from .offset_dt import offset_rings
        out = offset_rings(valid, spacing)
        if out:
            return out
    except Exception as exc:
        params.warn("distance-field offset failed (%s); trying miter inset" % exc)
    return _miter_contour_rings(valid, spacing)


def _connect_spiral(rings: List[Ring], spacing: float) -> List[Polyline]:
    """Connect concentric rings into as few continuous strokes as possible.

    Greedy nearest-ring walk with per-ring re-rooting: from the current stroke's
    end, jump to the nearest vertex of the nearest remaining ring and append it;
    when the nearest jump exceeds a few spacings the rings belong to a separate
    nest, so flush the stroke and start a new one. Result: one stroke per nested
    region, i.e. the fewest tool lifts possible (doc 03 s3.5)."""
    cores: List[Ring] = []
    for ring in rings:
        core = ring[:-1] if (len(ring) >= 2 and G._dist(ring[0], ring[-1]) < 1e-9) else list(ring)
        if len(core) >= 3:
            cores.append(core)
    if not cores:
        return []
    threshold = spacing * 3.0
    remaining = list(range(len(cores)))
    chains: List[Polyline] = []
    cur: Optional[Polyline] = None

    def sample_indices(n: int):
        step = max(1, n // 128)          # cap the nearest-vertex scan on huge rings
        return range(0, n, step)

    while remaining:
        if cur is None:
            idx = remaining.pop(0)       # offset_rings yields outermost first
            core = cores[idx]
            cur = core + [core[0]]
            continue
        end = cur[-1]
        best_ri = best_vi = -1
        best_d = float("inf")
        for ri in remaining:
            core = cores[ri]
            for vi in sample_indices(len(core)):
                d = G._dist(end, core[vi])
                if d < best_d:
                    best_d, best_ri, best_vi = d, ri, vi
        if best_d > threshold:
            chains.append(cur)
            cur = None
            continue
        remaining.remove(best_ri)
        core = cores[best_ri]
        seq = core[best_vi:] + core[:best_vi]
        seq.append(seq[0])               # trace the full ring back to its entry
        cur.extend(seq)
    if cur:
        chains.append(cur)
    return chains


class ContourFill(FillStrategy):
    name = "contour"

    def generate(self, rings: List[Ring], params: FillParams) -> List[Polyline]:
        if not rings:
            return []
        try:
            contour = _contour_rings(rings, params.spacing, params)
        except Exception:
            contour = None
        if not contour:
            from .base import zigzag_polylines
            return zigzag_polylines(rings, params.angle, params.spacing)
        return contour


class SpiralFill(FillStrategy):
    name = "spiral"

    def generate(self, rings: List[Ring], params: FillParams) -> List[Polyline]:
        if not rings:
            return []
        try:
            contour = _contour_rings(rings, params.spacing, params)
        except Exception:
            contour = None
        if not contour:
            params.warn("Spiral fill could not offset this shape; falling back to Zigzag")
            from .base import zigzag_polylines
            return zigzag_polylines(rings, params.angle, params.spacing)
        return _connect_spiral(contour, params.spacing)
