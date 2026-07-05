"""Centerline (medial axis) extraction for single-stroke text engraving.

Quality approach used by CAM/skeleton tools, not raster thinning (which makes the
stroke wobble inside its width). Pipeline per glyph:

  1. sample points densely along the glyph contours,
  2. Delaunay-triangulate them (Bowyer-Watson, pure numpy),
  3. keep triangles whose centroid is inside the filled region (handles holes and
     the gaps between glyphs via the even-odd rule),
  4. Chordal Axis Transform: connect midpoints of the internal (chord) edges
     - sleeve triangle (2 chords): midpoint-to-midpoint,
     - junction triangle (3 chords): centroid-to-each-midpoint,
     - terminal triangle (1 chord): midpoint-to-tip (so strokes reach their ends),
  5. link the axis segments into polylines, prune short corner spurs,
  6. Chaikin smoothing.

Because the chords across a uniform-width stroke are ~perpendicular, their midpoints
lie on the true centerline -> straight strokes give straight centerlines (no wobble).

Falls back to a raster skeleton if numpy/Delaunay is unavailable or fails.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import List

from . import geometry as G

Polyline = G.Polyline
Ring = G.Polyline

SMOOTH_ITERS = 2


def centerline(rings: List[Ring], log=None) -> List[Polyline]:
    closed = [G.close_ring(r) for r in rings if len(r) >= 4]
    if not closed:
        return []
    try:
        import numpy  # noqa: F401
    except Exception:
        return _raster_fallback(closed, log)

    try:
        out: List[Polyline] = []
        for glyph in _group_glyphs(closed):
            out.extend(_glyph_centerline(glyph, log))
        if out:
            return out
    except Exception as exc:
        if log:
            log("WARN", "centerline (medial axis) failed (%s); using raster fallback" % exc)
    return _raster_fallback(closed, log)


# ----------------------------------------------------- glyph grouping
def _ring_point_inside(inner: Ring, outer: Ring) -> bool:
    # representative interior point of `inner`: average of a few vertices
    n = min(len(inner) - 1, 6)
    cx = sum(inner[i][0] for i in range(n)) / n
    cy = sum(inner[i][1] for i in range(n)) / n
    return G.point_in_rings((cx, cy), [outer])


def _group_glyphs(rings: List[Ring]) -> List[List[Ring]]:
    """Group contours into glyphs: each outer contour plus the holes inside it."""
    n = len(rings)
    areas = [abs(G.polygon_area(r)) for r in rings]
    container = [-1] * n
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if areas[j] > areas[i] and _ring_point_inside(rings[i], rings[j]):
                if container[i] == -1 or areas[j] < areas[container[i]]:
                    container[i] = j
    groups = []
    for i in range(n):
        if container[i] == -1:
            grp = [rings[i]] + [rings[k] for k in range(n) if container[k] == i]
            groups.append(grp)
    return groups or [rings]


# ----------------------------------------------------- per-glyph axis
def _sample(rings: List[Ring], step: float):
    import numpy as np
    pts = []
    for r in rings:
        for i in range(len(r) - 1):
            a = r[i]
            b = r[i + 1]
            d = math.hypot(b[0] - a[0], b[1] - a[1])
            if d < 1e-9:
                continue
            k = max(1, int(d / step))
            for s in range(k):
                t = s / k
                pts.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    # dedupe
    seen = set()
    uniq = []
    for p in pts:
        key = (round(p[0], 4), round(p[1], 4))
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return np.array(uniq, dtype=float)


def _glyph_centerline(rings: List[Ring], log) -> List[Polyline]:
    import numpy as np

    stroke_w = _stroke_width(rings)
    step = max(stroke_w / 3.5, 0.12)
    P = _sample(rings, step)
    if len(P) < 4:
        return []

    tris = _delaunay(P)
    if not tris:
        return []

    # keep triangles whose centroid lies inside the filled region (even-odd)
    interior = []
    for (a, b, c) in tris:
        cx = (P[a][0] + P[b][0] + P[c][0]) / 3.0
        cy = (P[a][1] + P[b][1] + P[c][1]) / 3.0
        if G.point_in_rings((cx, cy), rings):
            interior.append((a, b, c))
    if not interior:
        return []

    # internal edges are shared by two interior triangles
    edge2tri = defaultdict(list)
    for ti, (a, b, c) in enumerate(interior):
        for e in ((a, b), (b, c), (c, a)):
            edge2tri[(min(e), max(e))].append(ti)

    def mid(e):
        return ((P[e[0]][0] + P[e[1]][0]) / 2.0, (P[e[0]][1] + P[e[1]][1]) / 2.0)

    segs = []  # (pointA, pointB)
    for (a, b, c) in interior:
        verts = (a, b, c)
        tri_edges = ((a, b), (b, c), (c, a))
        internal = [(min(e), max(e)) for e in tri_edges if len(edge2tri[(min(e), max(e))]) == 2]
        mids = [mid(e) for e in internal]
        if len(internal) == 2:
            segs.append((mids[0], mids[1]))
        elif len(internal) == 3:
            cen = (sum(P[v][0] for v in verts) / 3.0, sum(P[v][1] for v in verts) / 3.0)
            for m in mids:
                segs.append((cen, m))
        elif len(internal) == 1:
            e = internal[0]
            tip = [v for v in verts if v not in e][0]
            segs.append((mids[0], (P[tip][0], P[tip][1])))

    if not segs:
        return []
    return _link_prune_smooth(segs, stroke_w)


# ----------------------------------------------------- Delaunay (Bowyer-Watson)
def _delaunay(P):
    import numpy as np
    n = len(P)
    minx, miny = P.min(0)
    maxx, maxy = P.max(0)
    dm = max(maxx - minx, maxy - miny) * 10.0 + 10.0
    midx, midy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    verts = [tuple(p) for p in P] + [
        (midx - 2 * dm, midy - dm), (midx, midy + 2 * dm), (midx + 2 * dm, midy - dm)]
    s0, s1, s2 = n, n + 1, n + 2
    tris = {(s0, s1, s2)}

    def in_circ(tri, px, py):
        ax, ay = verts[tri[0]]
        bx, by = verts[tri[1]]
        cx, cy = verts[tri[2]]
        o = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        a1, a2 = ax - px, ay - py
        b1, b2 = bx - px, by - py
        c1, c2 = cx - px, cy - py
        det = ((a1 * a1 + a2 * a2) * (b1 * c2 - c1 * b2)
               - (b1 * b1 + b2 * b2) * (a1 * c2 - c1 * a2)
               + (c1 * c1 + c2 * c2) * (a1 * b2 - b1 * a2))
        return det > 0 if o > 0 else det < 0

    for ip in range(n):
        px, py = verts[ip]
        bad = [t for t in tris if in_circ(t, px, py)]
        edge_count = {}
        for t in bad:
            for e in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
                key = (min(e), max(e))
                edge_count[key] = edge_count.get(key, 0) + 1
        for t in bad:
            tris.discard(t)
        for e, cnt in edge_count.items():
            if cnt == 1:
                tris.add((e[0], e[1], ip))

    return [t for t in tris if t[0] < n and t[1] < n and t[2] < n]


# ----------------------------------------------------- link / prune / smooth
def _link_prune_smooth(segs, stroke_w) -> List[Polyline]:
    q = 4  # quantize decimals for node merging
    adj = defaultdict(set)

    def node(p):
        return (round(p[0], q), round(p[1], q))

    for a, b in segs:
        na, nb = node(a), node(b)
        if na != nb:
            adj[na].add(nb)
            adj[nb].add(na)
    if not adj:
        return []

    visited = set()  # undirected edges
    polylines = []

    def walk(start, nxt):
        path = [start, nxt]
        visited.add(frozenset((start, nxt)))
        prev, cur = start, nxt
        while len(adj[cur]) == 2:
            cand = [x for x in adj[cur] if x != prev and frozenset((cur, x)) not in visited]
            if not cand:
                break
            nx = cand[0]
            visited.add(frozenset((cur, nx)))
            path.append(nx)
            prev, cur = cur, nx
        return path

    seeds = [nd for nd in adj if len(adj[nd]) != 2]
    for s in seeds:
        for nb in list(adj[s]):
            if frozenset((s, nb)) not in visited:
                polylines.append(walk(s, nb))
    for nd in adj:  # leftover loops
        for nb in list(adj[nd]):
            if frozenset((nd, nb)) not in visited:
                polylines.append(walk(nd, nb))

    # prune short corner spurs: a polyline that ends at a junction and is short
    deg = {nd: len(adj[nd]) for nd in adj}
    spur = stroke_w * 0.9
    kept = []
    for pl in polylines:
        length = sum(math.hypot(pl[i + 1][0] - pl[i][0], pl[i + 1][1] - pl[i][1])
                     for i in range(len(pl) - 1))
        ends_at_junction = deg.get(pl[0], 0) >= 3 or deg.get(pl[-1], 0) >= 3
        is_leaf = deg.get(pl[0], 0) == 1 or deg.get(pl[-1], 0) == 1
        if is_leaf and ends_at_junction and length < spur:
            continue
        kept.append(pl)

    out = []
    for pl in kept:
        is_loop = pl[0] == pl[-1]
        sm = _chaikin([(x, y) for x, y in pl], SMOOTH_ITERS, closed=is_loop)
        if len(sm) >= 2:
            out.append(sm)
    return out


# ----------------------------------------------------- shared helpers
def _stroke_width(rings: List[Ring]) -> float:
    area = abs(sum(G.polygon_area(r) for r in rings))
    perim = sum(G.polyline_length(r) for r in rings)
    if perim <= 1e-9:
        return 1.0
    w = 2.0 * area / perim
    return w if w > 1e-3 else 1.0


def _chaikin(pts: Polyline, iterations: int, closed: bool = False) -> Polyline:
    if len(pts) < 3:
        return pts
    for _ in range(iterations):
        new: Polyline = []
        n = len(pts)
        if closed:
            for i in range(n):
                p = pts[i]
                qq = pts[(i + 1) % n]
                new.append((0.75 * p[0] + 0.25 * qq[0], 0.75 * p[1] + 0.25 * qq[1]))
                new.append((0.25 * p[0] + 0.75 * qq[0], 0.25 * p[1] + 0.75 * qq[1]))
            new.append(new[0])
        else:
            new.append(pts[0])
            for i in range(n - 1):
                p, qq = pts[i], pts[i + 1]
                new.append((0.75 * p[0] + 0.25 * qq[0], 0.75 * p[1] + 0.25 * qq[1]))
                new.append((0.25 * p[0] + 0.75 * qq[0], 0.25 * p[1] + 0.75 * qq[1]))
            new.append(pts[-1])
        pts = new
    return pts


# ----------------------------------------------------- raster fallback
def _raster_fallback(rings: List[Ring], log) -> List[Polyline]:
    # Minimal Zhang-Suen fallback (only used if numpy/Delaunay path fails).
    import math as _m
    xmin, ymin, xmax, ymax = G.rings_bbox(rings)
    w, h = xmax - xmin, ymax - ymin
    if w <= 0 or h <= 0:
        return []
    res = max(_stroke_width(rings) / 6.0, 0.12)
    cols = int(_m.ceil(w / res)) + 2
    rows = int(_m.ceil(h / res)) + 2
    if rows * cols > 2_000_000:
        return []
    grid = bytearray(rows * cols)
    for r in range(1, rows - 1):
        y = ymin + (r - 1 + 0.5) * res
        xs = []
        for ring in rings:
            for i in range(len(ring) - 1):
                x1, y1 = ring[i]
                x2, y2 = ring[i + 1]
                if (y1 <= y < y2) or (y2 <= y < y1):
                    xs.append(x1 + (x2 - x1) * (y - y1) / (y2 - y1))
        xs.sort()
        for k in range(0, len(xs) - 1, 2):
            ca = max(1, int((xs[k] - xmin) / res) + 1)
            cb = min(cols - 2, int((xs[k + 1] - xmin) / res) + 1)
            for c in range(ca, cb + 1):
                grid[r * cols + c] = 1
    _zhang_suen(grid, rows, cols)
    chains = _trace(grid, rows, cols)
    out = []
    for ch in chains:
        pts = [(xmin + (c - 1 + 0.5) * res, ymin + (r - 1 + 0.5) * res) for (r, c) in ch]
        if len(pts) >= 2 and G.polyline_length(pts) >= res * 2.5:
            out.append(_chaikin(pts, 2))
    return out


def _zhang_suen(grid, rows, cols):
    def at(r, c):
        return grid[r * cols + c]
    changed = True
    while changed:
        changed = False
        for step in (0, 1):
            rem = []
            for r in range(1, rows - 1):
                for c in range(1, cols - 1):
                    if not grid[r * cols + c]:
                        continue
                    p = [at(r - 1, c), at(r - 1, c + 1), at(r, c + 1), at(r + 1, c + 1),
                         at(r + 1, c), at(r + 1, c - 1), at(r, c - 1), at(r - 1, c - 1)]
                    b = sum(p)
                    if b < 2 or b > 6:
                        continue
                    seq = p + [p[0]]
                    a = sum(1 for i in range(8) if seq[i] == 0 and seq[i + 1] == 1)
                    if a != 1:
                        continue
                    if step == 0:
                        if p[0] and p[2] and p[4]:
                            continue
                        if p[2] and p[4] and p[6]:
                            continue
                    else:
                        if p[0] and p[2] and p[6]:
                            continue
                        if p[0] and p[4] and p[6]:
                            continue
                    rem.append(r * cols + c)
            if rem:
                changed = True
                for idx in rem:
                    grid[idx] = 0


_NB = [(-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1)]


def _trace(grid, rows, cols):
    pix = {(r, c) for r in range(rows) for c in range(cols) if grid[r * cols + c]}

    def neigh(r, c):
        return [(r + dr, c + dc) for dr, dc in _NB if (r + dr, c + dc) in pix]
    used = set()
    chains = []

    def walk(s, f):
        path = [s, f]
        used.add(frozenset((s, f)))
        prev, cur = s, f
        while len(neigh(*cur)) == 2:
            nxt = [n for n in neigh(*cur) if n != prev and frozenset((cur, n)) not in used]
            if not nxt:
                break
            used.add(frozenset((cur, nxt[0])))
            path.append(nxt[0])
            prev, cur = cur, nxt[0]
        return path
    for s in [p for p in pix if len(neigh(*p)) != 2]:
        for nb in neigh(*s):
            if frozenset((s, nb)) not in used:
                chains.append(walk(s, nb))
    return chains
