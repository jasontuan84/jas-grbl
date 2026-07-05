"""Per-layer settings table widget.

The columns depend on the output mode:
  - Engraving: Power %, Speed (mm/min), Pass
  - Plotter:   Force (g), Speed (mm/s)
Switching mode rebuilds the columns in place, preserving the values already typed
(each row keeps a full LayerSetting; only the mode's own fields are shown/edited).

The numeric columns are plain right-aligned text Entries (no +/- stepper buttons) to
stay compact in the narrow GCode tab. Values are parsed and clamped on read.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .. import constants as C
from ..config import LayerSetting, MachineConfig


# Eye icon pair (shown / hidden) for the per-layer show/hide toggle. Resolved once
# against the active icon theme; falls back to a text glyph if no eye icon is present.
_EYE_ICONS_CACHE: list = []


def _eye_icon_names():
    """(shown_icon, hidden_icon) available in the theme, or None to use a text glyph."""
    if not _EYE_ICONS_CACHE:
        result = None
        try:
            theme = Gtk.IconTheme.get_default()
            for shown, hidden in (("view-reveal-symbolic", "view-conceal-symbolic"),
                                  ("eye-open-negative-filled-symbolic",
                                   "eye-not-looking-symbolic")):
                if theme.has_icon(shown) and theme.has_icon(hidden):
                    result = (shown, hidden)
                    break
        except Exception:
            result = None
        _EYE_ICONS_CACHE.append(result)
    return _EYE_ICONS_CACHE[0]


# Per-layer colour swatch palette (design mock uses one accent chip per layer). The
# SVG layers carry no reliable single colour, so we cycle this palette by row index -
# purely a visual key that lines rows up with the design.
_SWATCH_PALETTE = ["#e8710a", "#58a6ff", "#3fb950", "#c026d3",
                   "#e3a008", "#2b8fa8", "#b48cf5", "#f85149"]


def _make_swatch(color_hex: str) -> Gtk.DrawingArea:
    """A small rounded colour chip drawn with Cairo (10x10, 3px radius)."""
    da = Gtk.DrawingArea()
    da.set_size_request(12, 12)
    da.set_valign(Gtk.Align.CENTER)
    n = int(color_hex.lstrip("#"), 16)
    rgb = ((n >> 16 & 255) / 255.0, (n >> 8 & 255) / 255.0, (n & 255) / 255.0)

    def _draw(_w, cr):
        r, w, h = 3.0, 12.0, 12.0
        cr.new_sub_path()
        cr.arc(w - r, r, r, -1.5708, 0)
        cr.arc(w - r, h - r, r, 0, 1.5708)
        cr.arc(r, h - r, r, 1.5708, 3.1416)
        cr.arc(r, r, r, 3.1416, 4.7124)
        cr.close_path()
        cr.set_source_rgb(*rgb)
        cr.fill()
        return False

    da.connect("draw", _draw)
    return da


def _num_entry(chars: int) -> Gtk.Entry:
    e = Gtk.Entry()
    e.set_width_chars(chars)
    e.set_max_width_chars(chars)
    e.set_alignment(0.5)                    # centred, matching the design
    e.set_input_purpose(Gtk.InputPurpose.DIGITS)
    return e


def _read_int(entry: Gtk.Entry, default: int, lo: int, hi: int) -> int:
    try:
        v = int(float(entry.get_text().strip()))
    except (ValueError, TypeError):
        v = default
    return max(lo, min(hi, v))


class _Row:
    __slots__ = ("layer", "mode", "enable", "stroke_text",
                 "power", "speed", "passes", "force", "_eye_img")

    def __init__(self, layer, mode: str):
        self.layer = layer
        self.mode = mode
        # Show/hide toggle rendered as an eye icon (open = shown, slashed = hidden).
        self.enable = Gtk.ToggleButton()
        self.enable.set_relief(Gtk.ReliefStyle.NONE)   # flat, compact
        self.enable.get_style_context().add_class("jg-flat")   # no border/chip look
        self._eye_img = Gtk.Image() if _eye_icon_names() else None
        if self._eye_img is not None:
            self.enable.add(self._eye_img)
        self.enable.connect("toggled", lambda *_: self._sync_eye())
        self._sync_eye()
        self.stroke_text = Gtk.CheckButton()
        if mode == C.MODE_PLOTTER:
            self.force = _num_entry(4)
            self.speed = _num_entry(6)      # mm/s
            self.power = self.passes = None
        else:
            self.power = _num_entry(3)
            self.speed = _num_entry(6)      # mm/min
            self.passes = _num_entry(2)
            self.force = None

    def _sync_eye(self) -> None:
        """Reflect the toggle state on the eye icon (or a text glyph fallback)."""
        active = self.enable.get_active()
        icons = _eye_icon_names()
        if self._eye_img is not None and icons:
            self._eye_img.set_from_icon_name(
                icons[0] if active else icons[1], Gtk.IconSize.BUTTON)
        else:
            self.enable.set_label("\U0001F441" if active else "–")  # eye / en-dash
        self.enable.set_tooltip_text(
            "Layer shown - click to hide" if active else "Layer hidden - click to show")

    def load(self, s: LayerSetting, visible: bool) -> None:
        self.enable.set_active(s.enabled and visible)
        self.stroke_text.set_active(s.stroke_text)
        if self.mode == C.MODE_PLOTTER:
            self.force.set_text(str(s.force))
            self.speed.set_text(str(s.plotter_speed))
        else:
            self.power.set_text(str(s.power))
            self.speed.set_text(str(s.speed))
            self.passes.set_text(str(s.passes))

    def setting(self, base: LayerSetting) -> LayerSetting:
        """Return ``base`` with the currently-shown mode's fields overwritten from
        the widgets. Fields for the other mode are preserved untouched."""
        s = replace(base)
        s.enabled = self.enable.get_active()
        s.stroke_text = self.stroke_text.get_active()
        if self.mode == C.MODE_PLOTTER:
            s.force = _read_int(self.force, C.DEFAULT_PLOTTER_FORCE, 1, 5000)
            s.plotter_speed = _read_int(self.speed, C.DEFAULT_PLOTTER_SPEED, 1, 100000)
        else:
            s.power = _read_int(self.power, LayerSetting.power, 0, 100)
            s.speed = _read_int(self.speed, LayerSetting.speed, 1, 100000)
            s.passes = _read_int(self.passes, LayerSetting.passes, 1, 100)
        return s


class LayerTable(Gtk.Grid):
    HEADERS = {
        C.MODE_ENGRAVING: ["", "LAYER", "POWER %", "SPEED", "PASS", "STROKE"],
        C.MODE_PLOTTER: ["", "LAYER", "FORCE g", "SPEED", "STROKE"],
    }

    def __init__(self):
        super().__init__()
        # Zero spacing so the header bar reads as one continuous strip (design); cells
        # get their own padding via widget margins in _rebuild.
        self.set_column_spacing(0)
        self.set_row_spacing(0)
        self.mode = C.DEFAULT_MODE
        self.layers: List[object] = []
        self._settings: Dict[str, LayerSetting] = {}
        self._rows: List[_Row] = []

    # ------------------------------------------------------------------ data
    def populate(self, layers, config: MachineConfig) -> None:
        self.layers = list(layers)
        self.mode = config.mode
        self._settings = {l.layer_id: config.layer_setting(l.layer_id) for l in self.layers}
        self._rebuild()

    def set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        self._capture()            # keep whatever the user typed before switching
        self.mode = mode
        self._rebuild()

    def _capture(self) -> None:
        for row in self._rows:
            self._settings[row.layer.layer_id] = row.setting(
                self._settings.get(row.layer.layer_id, LayerSetting()))

    # --------------------------------------------------------------- rebuild
    def _rebuild(self) -> None:
        for child in list(self.get_children()):
            child.destroy()
        self._rows = []

        headers = self.HEADERS[self.mode]
        for col, text in enumerate(headers):
            lbl = Gtk.Label(label=text)
            # LAYER is left-aligned; the numeric/stroke columns are centred over their
            # (centred) cell widgets. FILL so the header bar background is continuous.
            lbl.set_xalign(0.0 if col == 1 else 0.5)
            lbl.set_halign(Gtk.Align.FILL)
            lbl.get_style_context().add_class("jg-th")
            self.attach(lbl, col, 0, 1, 1)

        if not self.layers:
            empty = Gtk.Label(label="No layers found in this document.")
            empty.set_xalign(0.0)
            empty.set_margin_top(10)
            empty.set_margin_bottom(10)
            empty.set_margin_start(12)
            self.attach(empty, 0, 1, len(headers), 1)
            self.show_all()
            return

        def _cell(widget):
            """Centre a numeric widget in its column with a little horizontal breathing
            room. No vertical margin - the entry's own padding sets the row height, so
            rows stay compact (extra top/bottom margin was bloating them)."""
            widget.set_halign(Gtk.Align.CENTER)
            widget.set_margin_start(6)
            widget.set_margin_end(6)
            return widget

        for i, layer in enumerate(self.layers, start=1):
            row = _Row(layer, self.mode)
            row.load(self._settings.get(layer.layer_id, LayerSetting()), layer.visible)

            name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            name_box.set_hexpand(True)
            name_box.set_margin_start(4)
            name_box.pack_start(
                _make_swatch(_SWATCH_PALETTE[(i - 1) % len(_SWATCH_PALETTE)]),
                False, False, 0)
            name = Gtk.Label(label=layer.label + ("" if layer.visible else "  (hidden)"))
            name.set_xalign(0.0)
            name_box.pack_start(name, True, True, 0)
            row.stroke_text.set_halign(Gtk.Align.CENTER)

            self.attach(row.enable, 0, i, 1, 1)
            self.attach(name_box, 1, i, 1, 1)
            if self.mode == C.MODE_PLOTTER:
                self.attach(_cell(row.force), 2, i, 1, 1)
                self.attach(_cell(row.speed), 3, i, 1, 1)
                self.attach(row.stroke_text, 4, i, 1, 1)
            else:
                self.attach(_cell(row.power), 2, i, 1, 1)
                self.attach(_cell(row.speed), 3, i, 1, 1)
                self.attach(_cell(row.passes), 4, i, 1, 1)
                self.attach(row.stroke_text, 5, i, 1, 1)
            self._rows.append(row)
        self.show_all()

    # ------------------------------------------------------------------ read
    def get_pairs(self) -> List[Tuple[object, LayerSetting]]:
        self._capture()
        return [(l, self._settings[l.layer_id]) for l in self.layers]

    def reset_defaults(self) -> None:
        self._settings = {l.layer_id: LayerSetting() for l in self.layers}
        for row in self._rows:
            row.load(self._settings[row.layer.layer_id], row.layer.visible)
