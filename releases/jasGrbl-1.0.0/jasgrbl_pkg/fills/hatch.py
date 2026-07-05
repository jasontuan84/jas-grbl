"""Hatch, Cross-Hatch and Zigzag fills - all built on the scanline engine."""

from __future__ import annotations

from typing import List

from .base import FillParams, FillStrategy, Polyline, Ring, hatch_segments, zigzag_polylines


class HatchFill(FillStrategy):
    name = "hatch"

    def generate(self, rings: List[Ring], params: FillParams) -> List[Polyline]:
        return hatch_segments(rings, params.angle, params.spacing)


class CrossHatchFill(FillStrategy):
    name = "crosshatch"

    def generate(self, rings: List[Ring], params: FillParams) -> List[Polyline]:
        a = hatch_segments(rings, params.angle, params.spacing)
        b = hatch_segments(rings, params.angle + 90.0, params.spacing)
        return a + b


class ZigzagFill(FillStrategy):
    name = "zigzag"

    def generate(self, rings: List[Ring], params: FillParams) -> List[Polyline]:
        return zigzag_polylines(rings, params.angle, params.spacing)
