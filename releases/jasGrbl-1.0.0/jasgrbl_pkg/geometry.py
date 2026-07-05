"""Geometry: pure polygon math, machine-coordinate mapping, and SVG extraction.

The pure math (MachineSpace, polygon helpers, bezier flattening) imports nothing
from inkex so it can be unit-tested standalone. SVG extraction imports inkex lazily
inside the function that needs it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import constants as C

Point = Tuple[float, float]
Polyline = List[Point]

# ---------------------------------------------------------------------------
# Machine coordinate mapping
# ---------------------------------------------------------------------------


@dataclass
class MachineSpace:
    """Maps SVG-millimetre coordinates (design space: top-left origin, Y down) into
    machine coordinates for the selected Home corner.

    Home is NOT just a start point - it defines the machine's whole coordinate
    system: where (0,0) is and which way X+/Y+ point. Each corner therefore needs
    its own transform (mirror and/or flip), per
    docs/knowledge/basic/machine-home-position-coordinate-system.md:

        Home           X+     Y+      machineX     machineY
        top-left       right  down    designX      designY
        top-right      left   down    W - designX  designY
        bottom-left    right  up      designX      H - designY
        bottom-right   left   up      W - designX  H - designY

    Do NOT "de-mirror" top-left / bottom-right: their mirror/flip is the CORRECT
    transform for a machine homed at that corner (a generic Y-up G-code viewer that
    assumes bottom-left will show them flipped, but the machine engraves upright)."""

    width_mm: float
    height_mm: float
    home: str = C.HOME_BOTTOM_LEFT

    def to_machine(self, x_mm: float, y_mm: float) -> Point:
        w, h = self.width_mm, self.height_mm
        if self.home == C.HOME_BOTTOM_LEFT:
            return (x_mm, h - y_mm)
        if self.home == C.HOME_TOP_LEFT:
            return (x_mm, y_mm)
        if self.home == C.HOME_BOTTOM_RIGHT:
            return (w - x_mm, h - y_mm)
        if self.home == C.HOME_TOP_RIGHT:
            return (w - x_mm, y_mm)
        return (x_mm, y_mm)

    def to_svg_mm(self, mx: float, my: float) -> Point:
        """Inverse mapping: machine coords -> SVG millimetres."""
        w, h = self.width_mm, self.height_mm
        if self.home == C.HOME_BOTTOM_LEFT:
            return (mx, h - my)
        if self.home == C.HOME_TOP_LEFT:
            return (mx, my)
        if self.home == C.HOME_BOTTOM_RIGHT:
            return (w - mx, h - my)
        if self.home == C.HOME_TOP_RIGHT:
            return (w - mx, my)
        return (mx, my)

    def in_bounds(self, x: float, y: float, eps: float = 1e-6) -> bool:
        return (-eps <= x <= self.width_mm + eps) and (-eps <= y <= self.height_mm + eps)


# ---------------------------------------------------------------------------
# Polygon / polyline helpers
# ---------------------------------------------------------------------------


def bbox(points: List[Point]) -> Tuple[float, float, float, float]:
    """Return (xmin, ymin, xmax, ymax)."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def rings_bbox(rings: List[Polyline]) -> Tuple[float, float, float, float]:
    pts: List[Point] = []
    for r in rings:
        pts.extend(r)
    return bbox(pts)


def polygon_area(ring: Polyline) -> float:
    """Signed area (shoelace). Positive = counter-clockwise in math axes."""
    n = len(ring)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def point_in_rings(pt: Point, rings: List[Polyline]) -> bool:
    """Even-odd point-in-polygon test across all rings (outer + holes)."""
    x, y = pt
    inside = False
    for ring in rings:
        n = len(ring)
        if n < 3:
            continue
        j = n - 1
        for i in range(n):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if (yi > y) != (yj > y):
                xint = (xj - xi) * (y - yi) / (yj - yi) + xi
                if x < xint:
                    inside = not inside
            j = i
    return inside


def rotate(points: List[Point], angle_deg: float, cx: float = 0.0, cy: float = 0.0) -> List[Point]:
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    out = []
    for x, y in points:
        dx, dy = x - cx, y - cy
        out.append((cx + dx * ca - dy * sa, cy + dx * sa + dy * ca))
    return out


def close_ring(ring: Polyline) -> Polyline:
    """Ensure the first and last point coincide (for fill polygons)."""
    if len(ring) >= 2 and _dist(ring[0], ring[-1]) > 1e-9:
        return ring + [ring[0]]
    return ring


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def clean_polyline(pts: Polyline, eps: float = 1e-9) -> Polyline:
    """Drop non-finite (NaN/inf) points and consecutive duplicates.

    A single degenerate coordinate must never reach the emitter (it would produce
    'G1 Xnan'); zero-length segments break tangent/normal math downstream. Uses a
    tiny epsilon so intentionally-distinct flattened points are preserved
    (doc 12 s3.1 / s4)."""
    out: Polyline = []
    for p in pts:
        if not (math.isfinite(p[0]) and math.isfinite(p[1])):
            continue
        if out and _dist(out[-1], p) <= eps:
            continue
        out.append(p)
    return out


def polyline_length(pl: Polyline) -> float:
    return sum(_dist(pl[i], pl[i + 1]) for i in range(len(pl) - 1))


def convex_hull(points: List[Point]) -> List[Point]:
    """Andrew's monotone-chain convex hull (CCW), no repeated last point."""
    pts = sorted(set((round(p[0], 6), round(p[1], 6)) for p in points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: List[Point] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: List[Point] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def min_area_obb(hull: List[Point]) -> Tuple[float, float, float]:
    """Minimum-area oriented bounding box of a convex hull (rotating calipers via the
    hull-edge theorem). Returns (long, short, theta_deg) where theta is the LONG-axis
    direction in degrees."""
    n = len(hull)
    if n < 2:
        return (0.0, 0.0, 0.0)
    best = float("inf")
    res = (0.0, 0.0, 0.0)
    for i in range(n):
        ax, ay = hull[i]
        bx, by = hull[(i + 1) % n]
        ex, ey = bx - ax, by - ay
        length = math.hypot(ex, ey)
        if length < 1e-12:
            continue
        ux, uy = ex / length, ey / length
        vx, vy = -uy, ux
        minu = minv = float("inf")
        maxu = maxv = float("-inf")
        for px, py in hull:
            pu = px * ux + py * uy
            pv = px * vx + py * vy
            minu, maxu = min(minu, pu), max(maxu, pu)
            minv, maxv = min(minv, pv), max(maxv, pv)
        w, h = maxu - minu, maxv - minv
        area = w * h
        if area < best:
            best = area
            if w >= h:
                res = (w, h, math.degrees(math.atan2(uy, ux)))
            else:
                res = (h, w, math.degrees(math.atan2(vy, vx)))
    return res


# ---------------------------------------------------------------------------
# Bezier flattening (cubic) - recursive subdivision to a flatness tolerance
# ---------------------------------------------------------------------------


def flatten_cubic(p0: Point, p1: Point, p2: Point, p3: Point, tol: float, out: List[Point]) -> None:
    """Append flattened points of a cubic bezier (excluding p0, including p3)."""
    # Flatness: max distance of control points from the chord p0-p3.
    d1 = _point_line_dist(p1, p0, p3)
    d2 = _point_line_dist(p2, p0, p3)
    if max(d1, d2) <= tol or _dist(p0, p3) < 1e-9 and max(d1, d2) <= tol:
        out.append(p3)
        return
    # de Casteljau subdivision at t = 0.5
    p01 = _mid(p0, p1)
    p12 = _mid(p1, p2)
    p23 = _mid(p2, p3)
    p012 = _mid(p01, p12)
    p123 = _mid(p12, p23)
    pm = _mid(p012, p123)
    flatten_cubic(p0, p01, p012, pm, tol, out)
    flatten_cubic(pm, p123, p23, p3, tol, out)


def _mid(a: Point, b: Point) -> Point:
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def _point_line_dist(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    seg = math.hypot(dx, dy)
    if seg < 1e-12:
        return _dist(p, a)
    return abs((px - ax) * dy - (py - ay) * dx) / seg


# ---------------------------------------------------------------------------
# SVG extraction (inkex)
# ---------------------------------------------------------------------------

DRAW_TAGS = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon"}


@dataclass
class ShapeData:
    """A drawable element flattened to subpaths in SVG millimetres (top-left, Y down)."""

    element_id: str
    subpaths: List[Polyline]          # each polyline in mm
    closed: List[bool]                # parallel to subpaths
    has_fill: bool = False
    has_stroke: bool = False
    is_text: bool = False
    fill_gray: float = 0.0            # perceptual luminance 0=black .. 1=white (0 => densest fill)


@dataclass
class LayerData:
    layer_id: str
    label: str
    visible: bool
    shapes: List[ShapeData] = field(default_factory=list)
    has_text: bool = False


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _is_white_or_none(value: Optional[str]) -> bool:
    if not value:
        return True
    v = value.strip().lower()
    if v in ("none", "transparent"):
        return True
    if v in ("white", "#fff", "#ffffff"):
        return True
    if v.replace(" ", "") in ("rgb(255,255,255)", "rgb(100%,100%,100%)"):
        return True
    return False


def _fill_luminance(fill: Optional[str]) -> float:
    """Perceptual luminance of a fill colour, 0.0 (black) .. 1.0 (white).

    Drives shade-by-colour fill density (doc 03 s2): a darker fill reads as denser,
    so it gets tighter line spacing; a lighter fill gets sparser lines. An absent
    fill is the SVG default black (0.0 => densest). Uses inkex's colour parser when
    available, with a small hex/rgb/named fallback so it still works standalone."""
    if fill is None:
        return 0.0
    v = fill.strip().lower()
    if v in ("none", "transparent"):
        return 1.0
    rgb = None
    try:
        import inkex
        c = inkex.Color(fill)
        rgb = tuple(c.to_rgb()) if hasattr(c, "to_rgb") else (c[0], c[1], c[2])
    except Exception:
        rgb = _parse_rgb_fallback(v)
    if not rgb:
        return 0.0
    r, g, b = rgb[0], rgb[1], rgb[2]
    return max(0.0, min(1.0, (0.299 * r + 0.587 * g + 0.114 * b) / 255.0))


_NAMED_COLORS = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "green": (0, 128, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
    "gray": (128, 128, 128), "grey": (128, 128, 128), "silver": (192, 192, 192),
}


def _parse_rgb_fallback(v: str):
    if v in _NAMED_COLORS:
        return _NAMED_COLORS[v]
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        if len(h) >= 6:
            try:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            except ValueError:
                return None
    if v.startswith("rgb("):
        try:
            parts = v[4:].rstrip(")").split(",")
            vals = []
            for p in parts[:3]:
                p = p.strip()
                if p.endswith("%"):
                    vals.append(round(float(p[:-1]) * 255.0 / 100.0))
                else:
                    vals.append(int(float(p)))
            if len(vals) == 3:
                return tuple(vals)
        except ValueError:
            return None
    return None


def _style_fill_stroke(element):
    """Return (has_fill, has_stroke, fill_gray) honoring cascaded style."""
    try:
        style = element.effective_style()
    except Exception:
        style = element.style
    fill = style.get("fill")
    stroke = style.get("stroke")
    # Per SVG spec the default fill is black (opaque) when none is specified, so an
    # element with no fill at all (e.g. text converted to a path) is still engravable.
    # Only an explicit 'none'/'white' fill counts as not-filled.
    if fill is None:
        has_fill = True
    else:
        has_fill = not _is_white_or_none(fill)
    has_stroke = bool(stroke) and stroke.strip().lower() != "none"
    return has_fill, has_stroke, _fill_luminance(fill)


def svg_has_text(svg) -> bool:
    """True if the document contains any text or flowed-text elements."""
    try:
        return bool(svg.xpath("//svg:text | //svg:flowRoot"))
    except Exception:
        return False


def _text_wants_fill(fill) -> bool:
    """Decide whether a piece of text should be engraved as a HATCH FILL (True) or
    as an OUTLINE (False), from its resolved fill:
      - no fill declared  -> default is black -> fill (hatch)
      - explicit white / none / transparent -> outline
      - any other colour -> fill (hatch)
    """
    if fill is None:
        return True
    return not _is_white_or_none(fill)


def text_to_path_root(svg, log=None):
    """Return a COPY of the document with text (and flowRoot) converted to paths,
    by running Inkscape headlessly. The original ``svg`` is left untouched so the
    user's editable text is preserved. Returns None on failure.

    Text whose fill is white/none is first re-styled (on the copy) to
    ``fill:none;stroke`` so it converts into an OUTLINE that we trace, instead of a
    filled region we hatch. Text with a visible (non-white) fill stays filled -> hatch.
    """
    import inkex
    from lxml import etree

    from inkex.command import inkscape_command

    # Work on a detached, fully-parsed copy so the live document is never modified.
    try:
        work = inkex.load_svg(etree.tostring(svg)).getroot()
    except Exception:
        work = svg  # fall back to in-place (still only serialized by the command)

    # Bake outline-intent for white/none-fill text.
    for t in work.xpath("//svg:text | //svg:flowRoot"):
        try:
            resolved = t.effective_style().get("fill")
        except Exception:
            resolved = t.style.get("fill") if hasattr(t, "style") else None
        if not _text_wants_fill(resolved):
            stl = t.style
            stl["fill"] = "none"
            if not stl.get("stroke") or str(stl.get("stroke")).strip().lower() == "none":
                stl["stroke"] = "#000000"
                if not stl.get("stroke-width"):
                    stl["stroke-width"] = "0.3"
            t.style = stl

    # flowRoot first (it contains rectangles), then text; then save (export-do).
    actions = (
        "unlock-all;"
        "select-by-element:flowRoot;object-to-path;select-clear;"
        "select-by-element:text;object-to-path;select-clear;"
        "export-overwrite;export-do"
    )
    try:
        data = inkscape_command(work, actions=actions)
        return inkex.load_svg(data).getroot()
    except Exception as exc:
        if log:
            log("WARN", "auto text-to-path failed (%s); text layers will be skipped. "
                        "Tip: select the text, run Path > Object to Path, then Generate" % exc)
        return None


def document_size_mm(svg, uu_per_mm: float) -> Tuple[float, float]:
    """Return the Inkscape document (viewBox) size in millimetres.

    The value is expressed in the same SVG-millimetre frame that ``extract_layers``
    produces (user units divided by ``uu_per_mm``), so the machine work area matches
    the document exactly and shapes keep their document position. Falls back to the
    default board size when the document size cannot be read."""
    try:
        w = float(svg.viewbox_width) / uu_per_mm
        h = float(svg.viewbox_height) / uu_per_mm
        if w > 0 and h > 0:
            return (w, h)
    except Exception:
        pass
    return (C.DEFAULT_BOARD_WIDTH_MM, C.DEFAULT_BOARD_HEIGHT_MM)


def extract_layers(svg, uu_per_mm: float, flatness_mm: float) -> List[LayerData]:
    """Walk the document, returning one LayerData per Inkscape layer.

    Coordinates are converted to millimetres in the SVG frame (top-left origin,
    Y pointing down). Group/layer transforms are honored via composed_transform.
    """
    import inkex  # lazy: only needed when actually running inside Inkscape

    flat_uu = max(flatness_mm * uu_per_mm, 1e-3)
    layers: List[LayerData] = []

    layer_nodes = svg.xpath('//svg:g[@inkscape:groupmode="layer"]')
    if not layer_nodes:
        # No explicit layers: treat the whole document as one synthetic layer.
        layer_nodes = [svg]
    else:
        # SVG document order is bottom -> top. Return top layer first so both the
        # settings table and the generation order run the topmost layer first.
        layer_nodes = list(reversed(layer_nodes))

    for node in layer_nodes:
        label = node.get("inkscape:label") or node.get("id") or "layer"
        lid = node.get("id") or label
        display = (node.style.get("display") if hasattr(node, "style") else None)
        visible = display != "none"
        ldata = LayerData(layer_id=lid, label=label, visible=visible)

        for el in node.iter():
            tag = _local_tag(el.tag)
            if tag == "text":
                ldata.has_text = True
                continue
            if tag not in DRAW_TAGS:
                continue
            # Skip elements that live inside a nested layer (counted separately).
            try:
                if el is not node and el.get("inkscape:groupmode") == "layer":
                    continue
            except Exception:
                pass
            shape = _shape_from_element(el, uu_per_mm, flat_uu)
            if shape and shape.subpaths:
                ldata.shapes.append(shape)
        layers.append(ldata)

    return layers


def _shape_from_element(el, uu_per_mm: float, flat_uu: float) -> Optional[ShapeData]:
    try:
        path = el.path.transform(el.composed_transform()).to_superpath()
    except Exception:
        return None

    subpaths: List[Polyline] = []
    closed_flags: List[bool] = []
    for sp in path:
        pts: List[Point] = []
        if not sp:
            continue
        first = sp[0][1]
        pts.append((first[0] / uu_per_mm, first[1] / uu_per_mm))
        for i in range(len(sp) - 1):
            p0 = sp[i][1]
            c1 = sp[i][2]
            c2 = sp[i + 1][0]
            p3 = sp[i + 1][1]
            flatten_cubic(
                (p0[0] / uu_per_mm, p0[1] / uu_per_mm),
                (c1[0] / uu_per_mm, c1[1] / uu_per_mm),
                (c2[0] / uu_per_mm, c2[1] / uu_per_mm),
                (p3[0] / uu_per_mm, p3[1] / uu_per_mm),
                flat_uu / uu_per_mm,
                pts,
            )
        pts = clean_polyline(pts)
        if len(pts) >= 2:
            closed = _dist(pts[0], pts[-1]) < (flat_uu / uu_per_mm) + 1e-9
            subpaths.append(pts)
            closed_flags.append(closed)

    if not subpaths:
        return None
    has_fill, has_stroke, fill_gray = _style_fill_stroke(el)
    return ShapeData(
        element_id=el.get("id") or "",
        subpaths=subpaths,
        closed=closed_flags,
        has_fill=has_fill,
        has_stroke=has_stroke,
        fill_gray=fill_gray,
    )
