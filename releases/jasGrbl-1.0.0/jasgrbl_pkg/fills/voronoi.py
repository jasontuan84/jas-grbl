"""Voronoi (decorative) fill. Requires scipy; falls back to Hatch when absent."""

from __future__ import annotations

import math
from typing import List

from .. import geometry as G
from .base import FillParams, FillStrategy, Polyline, Ring, hatch_segments


def _poisson_points(rings: List[Ring], spacing: float, limit: int = 4000) -> List[G.Point]:
    """Cheap blue-noise-ish sampling: jittered grid points kept inside the region."""
    xmin, ymin, xmax, ymax = G.rings_bbox(rings)
    step = max(spacing, 0.3)
    pts: List[G.Point] = []
    gy = ymin
    row = 0
    while gy <= ymax and len(pts) < limit:
        gx = xmin + (step / 2.0 if row % 2 else 0.0)
        while gx <= xmax and len(pts) < limit:
            # deterministic jitter (no RNG) so output is reproducible
            jx = (math.sin(gx * 12.9898 + gy * 78.233) * 0.5) * step * 0.4
            jy = (math.cos(gx * 39.346 + gy * 11.135) * 0.5) * step * 0.4
            p = (gx + jx, gy + jy)
            if G.point_in_rings(p, rings):
                pts.append(p)
            gx += step
        gy += step
        row += 1
    return pts


class VoronoiFill(FillStrategy):
    name = "voronoi"

    def generate(self, rings: List[Ring], params: FillParams) -> List[Polyline]:
        closed = [G.close_ring(r) for r in rings if len(r) >= 2]
        if not closed:
            return []
        try:
            from scipy.spatial import Voronoi  # type: ignore
        except Exception:
            params.warn("Voronoi fill needs scipy (not installed); falling back to Hatch")
            return hatch_segments(rings, params.angle, params.spacing)

        seeds = _poisson_points(closed, params.spacing)
        if len(seeds) < 4:
            params.warn("Voronoi fill: region too small for seeds; falling back to Hatch")
            return hatch_segments(rings, params.angle, params.spacing)

        vor = Voronoi(seeds)
        segments: List[Polyline] = []
        for (i, j) in vor.ridge_vertices:
            if i < 0 or j < 0:
                continue
            a = tuple(vor.vertices[i])
            b = tuple(vor.vertices[j])
            mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            if G.point_in_rings(mid, closed):
                segments.append([a, b])
        if not segments:
            params.warn("Voronoi fill produced no edges; falling back to Hatch")
            return hatch_segments(rings, params.angle, params.spacing)
        return segments
