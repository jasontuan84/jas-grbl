"""Robust inward-offset (contour-parallel) generation via a distance field.

Why not a per-vertex miter inset? That only survives ONE simple, mildly-concave
ring - it self-intersects on real concavity (stars, letters) and cannot handle
holes or several disjoint regions at all. The classic robust technique used by
CAM "peel"/contour-parallel toolpaths is instead:

    1. rasterise the even-odd interior into an inside/outside mask;
    2. compute the distance from every interior sample to the boundary
       (a chamfer distance transform - exact enough for offsetting);
    3. the inward offset by distance d is exactly the iso-line ``distance == d``.
       Extract iso-lines at d = spacing/2, 3*spacing/2, ... with marching squares.

This is topology-agnostic: holes, concavities and multiple nests all fall out for
free, because they are just features of the distance field. Everything is pure
Python with no external dependencies (so it behaves identically with or without
NumPy/SciPy installed) and is bounded by a hard cell cap. Output is a list of
CLOSED rings in machine millimetres, ready for Contour (draw each ring) or Spiral
(connect them, doc 03 s3.4-3.5).
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import List, Optional, Tuple

from .. import geometry as G

Ring = G.Polyline
Point = G.Point

# Sampling: the grid step is a fraction of the line spacing so iso-lines are smooth
# rather than staircased. spacing/3 balances fidelity against the cell budget.
CELLS_PER_SPACING = 3.0
# Hard cell cap so a huge region can't blow up memory/time; above it the grid is
# coarsened (contours get slightly rounder, never wrong). ~500k keeps pure-Python
# passes well under a second.
MAX_CELLS = 500_000
# Marching-squares node-merge quantisation (mm). Well below any real feature.
_Q = 4


def offset_rings(rings: List[Ring], spacing: float,
                 first_offset: Optional[float] = None) -> Optional[List[Ring]]:
    """Return concentric inward-offset CLOSED rings for an even-odd region.

    ``first_offset`` is how far inside the boundary the outermost ring sits
    (default spacing/2, doc 03 s3.4). Returns None when the region is degenerate
    or has no interior a ring could occupy, so the caller can fall back."""
    valid = [r for r in rings if len(r) >= 3]
    if not valid or spacing <= 0:
        return None
    xmin, ymin, xmax, ymax = G.rings_bbox(valid)
    w, h = xmax - xmin, ymax - ymin
    if w <= 0 or h <= 0:
        return None

    res = spacing / CELLS_PER_SPACING
    pad = 2.0 * spacing                          # border ring of guaranteed-outside cells
    # coarsen if the cell budget would be exceeded
    est_cols = (w + 2 * pad) / res
    est_rows = (h + 2 * pad) / res
    if est_cols * est_rows > MAX_CELLS:
        res = math.sqrt((w + 2 * pad) * (h + 2 * pad) / MAX_CELLS)
    x0, y0 = xmin - pad, ymin - pad
    cols = int(math.ceil((w + 2 * pad) / res)) + 1
    rows = int(math.ceil((h + 2 * pad) / res)) + 1
    if cols < 3 or rows < 3:
        return None

    mask = _inside_mask(valid, x0, y0, res, cols, rows)
    dist = _distance_transform(mask, cols, rows, res)

    d0 = first_offset if first_offset is not None else spacing / 2.0
    maxd = max(dist)
    if maxd < d0:                                # nothing is even half a spacing deep
        return None
    levels: List[float] = []
    d = d0
    while d <= maxd and len(levels) < 20000:
        levels.append(d)
        d += spacing
    if not levels:
        return None

    rings_out = _marching_squares_levels(dist, cols, rows, x0, y0, res, levels)
    rings_out = [G.close_ring(_chaikin_closed(r)) for r in rings_out if len(r) >= 4]
    return rings_out or None


# --------------------------------------------------------------- rasterisation
def _inside_mask(rings: List[Ring], x0: float, y0: float, res: float,
                 cols: int, rows: int) -> List[bool]:
    """Even-odd interior test sampled at every grid point P(r,c)=(x0+c*res, y0+r*res)."""
    mask = [False] * (rows * cols)
    for r in range(rows):
        y = y0 + r * res
        xs: List[float] = []
        for ring in rings:
            m = len(ring)
            for i in range(m - 1):
                x1, y1 = ring[i]
                x2, y2 = ring[i + 1]
                if (y1 <= y < y2) or (y2 <= y < y1):
                    xs.append(x1 + (x2 - x1) * (y - y1) / (y2 - y1))
        if len(xs) < 2:
            continue
        xs.sort()
        base = r * cols
        for k in range(0, len(xs) - 1, 2):
            ca = int(math.ceil((xs[k] - x0) / res))
            cb = int(math.floor((xs[k + 1] - x0) / res))
            if ca < 0:
                ca = 0
            if cb > cols - 1:
                cb = cols - 1
            for c in range(ca, cb + 1):
                mask[base + c] = True
    return mask


# ------------------------------------------------------------ distance field
def _distance_transform(mask: List[bool], cols: int, rows: int, res: float) -> List[float]:
    """Chamfer (1, sqrt2) two-pass distance from each interior cell to the nearest
    exterior cell, in millimetres. ~2-3% under true Euclidean - negligible for
    offset spacing, and cheap without NumPy."""
    INF = float(cols + rows) * res * 4.0
    a = res
    b = res * math.sqrt(2.0)
    dist = [0.0 if not mask[i] else INF for i in range(rows * cols)]

    # forward pass (top-left -> bottom-right)
    for r in range(rows):
        base = r * cols
        pbase = base - cols
        for c in range(cols):
            i = base + c
            if dist[i] == 0.0:
                continue
            best = dist[i]
            if c > 0 and dist[i - 1] + a < best:
                best = dist[i - 1] + a
            if r > 0:
                if dist[pbase + c] + a < best:
                    best = dist[pbase + c] + a
                if c > 0 and dist[pbase + c - 1] + b < best:
                    best = dist[pbase + c - 1] + b
                if c < cols - 1 and dist[pbase + c + 1] + b < best:
                    best = dist[pbase + c + 1] + b
            dist[i] = best
    # backward pass (bottom-right -> top-left)
    for r in range(rows - 1, -1, -1):
        base = r * cols
        nbase = base + cols
        for c in range(cols - 1, -1, -1):
            i = base + c
            if dist[i] == 0.0:
                continue
            best = dist[i]
            if c < cols - 1 and dist[i + 1] + a < best:
                best = dist[i + 1] + a
            if r < rows - 1:
                if dist[nbase + c] + a < best:
                    best = dist[nbase + c] + a
                if c > 0 and dist[nbase + c - 1] + b < best:
                    best = dist[nbase + c - 1] + b
                if c < cols - 1 and dist[nbase + c + 1] + b < best:
                    best = dist[nbase + c + 1] + b
            dist[i] = best
    return dist


# ---------------------------------------------------------- marching squares
def _marching_squares_levels(dist: List[float], cols: int, rows: int,
                             x0: float, y0: float, res: float,
                             levels: List[float]) -> List[Ring]:
    """One pass over all cells; each cell emits crossing segments for every iso
    level that falls between its min and max corner value (so the work is
    O(cells + total_contour_length), not O(cells * levels))."""
    lo = levels[0]
    inv = 1.0 / (levels[1] - levels[0]) if len(levels) > 1 else 0.0
    nlev = len(levels)
    segs_by_level: List[List[Tuple[Point, Point]]] = [[] for _ in range(nlev)]

    for r in range(rows - 1):
        b0 = r * cols
        b1 = b0 + cols
        yT = y0 + r * res
        yB = yT + res
        for c in range(cols - 1):
            d00 = dist[b0 + c]
            d10 = dist[b0 + c + 1]
            d11 = dist[b1 + c + 1]
            d01 = dist[b1 + c]
            cmin = d00
            if d10 < cmin: cmin = d10
            if d11 < cmin: cmin = d11
            if d01 < cmin: cmin = d01
            cmax = d00
            if d10 > cmax: cmax = d10
            if d11 > cmax: cmax = d11
            if d01 > cmax: cmax = d01
            if cmax <= cmin:
                continue
            # levels strictly inside (cmin, cmax]
            k_lo = int(math.ceil((cmin - lo) * inv)) if inv else 0
            if k_lo < 0:
                k_lo = 0
            xL = x0 + c * res
            xR = xL + res
            for k in range(k_lo, nlev):
                L = levels[k]
                if L <= cmin:
                    continue
                if L > cmax:
                    break
                seg = _cell_segments(d00, d10, d11, d01, L, xL, xR, yT, yB)
                if seg:
                    segs_by_level[k].extend(seg)

    out: List[Ring] = []
    for segs in segs_by_level:
        if segs:
            out.extend(_link_rings(segs))
    return out


def _interp(pa: Point, va: float, pb: Point, vb: float, L: float) -> Point:
    if vb == va:
        return pa
    t = (L - va) / (vb - va)
    return (pa[0] + (pb[0] - pa[0]) * t, pa[1] + (pb[1] - pa[1]) * t)


def _cell_segments(d00: float, d10: float, d11: float, d01: float, L: float,
                   xL: float, xR: float, yT: float, yB: float):
    """Marching-squares crossings for one cell. Corners: TL,TR,BR,BL. Two edge
    crossings -> one segment; four (saddle) -> resolve by the cell-average."""
    TL = (xL, yT); TR = (xR, yT); BR = (xR, yB); BL = (xL, yB)
    aTL = d00 >= L; aTR = d10 >= L; aBR = d11 >= L; aBL = d01 >= L
    pts = []
    if aTL != aTR:
        pts.append(("T", _interp(TL, d00, TR, d10, L)))
    if aTR != aBR:
        pts.append(("R", _interp(TR, d10, BR, d11, L)))
    if aBR != aBL:
        pts.append(("B", _interp(BR, d11, BL, d01, L)))
    if aBL != aTL:
        pts.append(("L", _interp(BL, d01, TL, d00, L)))
    if len(pts) == 2:
        return [(pts[0][1], pts[1][1])]
    if len(pts) == 4:
        pT = pR = pB = pL = None
        for e, p in pts:
            if e == "T": pT = p
            elif e == "R": pR = p
            elif e == "B": pB = p
            else: pL = p
        center = (d00 + d10 + d11 + d01) * 0.25
        if center >= L:
            return [(pT, pR), (pB, pL)]
        return [(pT, pL), (pR, pB)]
    return []


def _link_rings(segs: List[Tuple[Point, Point]]) -> List[Ring]:
    """Chain marching-squares segments (undirected) into closed rings by matching
    quantised endpoints. Iso-lines of a field on a bounded grid are always closed."""
    adj = defaultdict(list)
    coord = {}

    def key(p: Point):
        return (round(p[0], _Q), round(p[1], _Q))

    for a, b in segs:
        ka, kb = key(a), key(b)
        if ka == kb:
            continue
        adj[ka].append(kb)
        adj[kb].append(ka)
        coord[ka] = a
        coord[kb] = b

    used = set()   # frozenset({ka, kb})
    rings: List[Ring] = []
    for start in list(adj.keys()):
        for first in adj[start]:
            e0 = frozenset((start, first))
            if e0 in used:
                continue
            used.add(e0)
            chain = [start, first]
            prev, cur = start, first
            while cur != start:
                nxt = None
                for cand in adj[cur]:
                    if cand == prev:
                        continue
                    if frozenset((cur, cand)) in used:
                        continue
                    nxt = cand
                    break
                if nxt is None:
                    break
                used.add(frozenset((cur, nxt)))
                chain.append(nxt)
                prev, cur = cur, nxt
            if len(chain) >= 4:
                ring = [coord[k] for k in chain]
                if G._dist(ring[0], ring[-1]) > 1e-9:
                    ring.append(ring[0])
                rings.append(ring)
    return rings


def _chaikin_closed(pts: Ring, iterations: int = 1) -> Ring:
    """Light corner-cutting to shave the marching-squares staircase off a closed
    ring. One pass is enough at spacing/3 resolution."""
    if len(pts) < 4:
        return pts
    ring = pts[:-1] if G._dist(pts[0], pts[-1]) < 1e-9 else list(pts)
    n = len(ring)
    if n < 3:
        return pts
    for _ in range(iterations):
        new: Ring = []
        m = len(ring)
        for i in range(m):
            p = ring[i]
            q = ring[(i + 1) % m]
            new.append((0.75 * p[0] + 0.25 * q[0], 0.75 * p[1] + 0.25 * q[1]))
            new.append((0.25 * p[0] + 0.75 * q[0], 0.25 * p[1] + 0.75 * q[1]))
        ring = new
    ring.append(ring[0])
    return ring
