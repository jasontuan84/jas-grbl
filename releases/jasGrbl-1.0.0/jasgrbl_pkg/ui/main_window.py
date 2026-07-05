"""The jasGrbl main dialog.

Layout (>=1000px wide):
  - Left  (~60%): toolpath Preview canvas (Cairo) - axes, grid, blue toolpath.
  - Right (~40%): Notebook
      Tab "Generate": per-layer table, fill controls, Engraving|Plotter mode +
                      GRBL|Vinyl Cutter machine profile, Generate / Clear / Save,
                      plus a small generation-status log (NOT the serial log).
      Tab "GRBL"    : Connect + Jog (directional pad, feed, step, job Start/
                      Frame/Test-Laser) side by side, above the Serial Log. A live X/Y
                      readout tracks the true work position parsed from GRBL '?' status
                      reports.
      Tab "VINYL"   : mirrors the GRBL tab (Connect + Jog side by side) but tuned for
                      a vinyl cutter - no Serial Log; a "Reset Machine" button instead
                      of "Grbl Setting"; "Test Cut" instead of "Test Laser"; no
                      Continuous toggle; Jog Feed default 500; "Set Origin" instead of
                      "Set Home"; and a "Cut Setting" frame (Speed mm/s, Force g) where
                      the Serial Log sits on the GRBL tab.
      Tab "Error Log": always last, always visible. A searchable, newest-first table
                      (DateTime, Actor, Message) of every error the extension hits, with
                      a Clear Log button and a red unseen-count badge on the tab.
    Only the machine tab matching the current profile (GRBL vs Vinyl Cutter) is
    shown; the other is hidden. The Error Log tab is always shown.

While a job streams, all setting/generate/connect/jog controls are disabled (only the
Stop button stays live) and a "Sending…" spinner overlays the preview; a stream error
stops immediately, pops an alert, and is recorded in the Error Log.

There is no SVG simulation layer anymore - the preview lives entirely in the dialog.
"""

from __future__ import annotations

import os
import tempfile
import threading
from datetime import datetime
from typing import List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from .. import __version__
from .. import constants as C
from .. import geometry as G
from .. import serial_io
from ..config import LayerSetting, MachineConfig
from ..fills import FillParams
from ..gcode import GenOptions, emit_grbl, emit_hpgl, plan_toolpaths
from .error_log import ErrorLogView
from .layer_table import LayerTable
from .log_view import LogView
from .preview import PreviewCanvas
from .stepper import Stepper
from .style import apply_css

TEMP_GCODE_PATH = os.path.join(tempfile.gettempdir(), "jasgrbl_temp.gcode")
TEMP_HPGL_PATH = os.path.join(tempfile.gettempdir(), "jasgrbl_temp" + C.HPGL_EXT)


def _left_label(text: str) -> Gtk.Label:
    lab = Gtk.Label(label=text)
    lab.set_xalign(0.0)
    return lab


def _uu_per_mm(svg) -> float:
    try:
        value = float(svg.unittouu("1mm"))
        if value > 0:
            return value
    except Exception:
        pass
    return 96.0 / 25.4


class JasGrblWindow(Gtk.Window):
    def __init__(self, svg):
        super().__init__(title=C.WINDOW_TITLE)
        apply_css()
        # Scope every custom style rule under this class so GTK's own dialogs keep the
        # native theme (see ui/style.py).
        self.get_style_context().add_class("jg-root")
        self.svg = svg
        self.config = MachineConfig.load()
        self.uu_per_mm = _uu_per_mm(svg)
        # Work area follows the Inkscape document; the design keeps its position.
        self.doc_w_mm, self.doc_h_mm = G.document_size_mm(svg, self.uu_per_mm)
        self.program = None
        self.mode = self.config.mode if self.config.mode in C.MODES else C.DEFAULT_MODE
        self.config.mode = self.mode          # normalise any stale/invalid stored value
        # Machine profile drives the Home corner and the export format.
        self.profile = self.config.profile if self.config.profile in C.PROFILES else C.DEFAULT_PROFILE
        self.config.profile = self.profile
        self.config.home = C.PROFILE_HOME[self.profile]
        self.temp_gcode_path = TEMP_GCODE_PATH
        self.temp_hpgl_path = TEMP_HPGL_PATH
        self._has_gcode = False
        self._has_hpgl = False
        self._busy = False
        # Streaming state: user_stopped distinguishes a Stop click from a real error;
        # last_error_msg feeds the error alert dialog. err_unseen drives the tab badge.
        self._user_stopped = False
        self._last_error_msg = ""
        self._err_unseen = 0
        # Open-loop estimate of the GRBL work position (mm), updated by jog/home/set-home
        # and shown in the Serial Log so the user can follow each action.
        # True work position parsed from GRBL status reports ('?' -> '<...|WPos:..>').
        self._grbl_x = 0.0
        self._grbl_y = 0.0
        self._grbl_state = ""
        self._grbl_pos_known = False           # True once a status report has arrived
        # Vinyl jog/origin follow the proven Plotter reference EXACTLY. Jog emits ABSOLUTE
        # HPGL (LH721 ignores relative PR), so we track the head in software. Set Origin is a
        # SOFTWARE offset - it sends NO command (the reference's setOriginHere just does
        # m_originAbsMm += m_headMm; head = 0); the offset is then added to every absolute
        # coordinate emitted (jog/home/frame/test/cut) so the cut lands where the head was
        # jogged. _v_head_mm = head relative to the work origin; _v_origin_mm = machine
        # position of work (0,0). (HPGL IN does not re-origin the LH721, hence the offset.)
        self._v_head_mm = [0.0, 0.0]
        self._v_origin_mm = [0.0, 0.0]
        self.serial = serial_io.SerialManager(self._log_threadsafe,
                                              on_status=self._on_grbl_status)
        # Resume support: the prepared program of the last GRBL stream, so a job that
        # stops/errors partway can be resumed from the last acknowledged line.
        self._stream_program: List[str] = []
        self._resume_index = 0

        self.set_size_request(C.WINDOW_MIN_WIDTH, 640)
        self.set_default_size(1040, 720)
        self.set_border_width(0)
        self.connect("destroy", self._on_destroy)

        # The native OS title bar and window controls are left untouched (macOS traffic
        # lights on macOS, Windows buttons on Windows, etc.). The design's header strip
        # (centred "jas·GRBL" + connection status pill) is an in-content row at the top
        # of the window instead of a custom title bar.
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)
        outer.pack_start(self._build_top_bar(), False, False, 0)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.set_border_width(8)
        outer.pack_start(content, True, True, 0)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_wide_handle(True)          # room for the 3-dot resize grip (see CSS)
        content.pack_start(paned, True, True, 0)
        paned.pack1(self._build_preview_frame(), True, False)
        paned.pack2(self._build_right_notebook(), False, False)
        paned.set_position(620)

        # Collect everything to disable while a job streams (all but the Stop button).
        self._collect_stream_lock_widgets()

        self._extract_and_populate()
        self._refresh_ports()
        self._sync_preview_machine()
        self._sync_connect_button()

    # ====================================================== Top bar (in content)
    def _build_top_bar(self) -> Gtk.Widget:
        """The design's header strip, rendered as an in-content row (NOT a custom title
        bar) so the OS keeps its native title bar + window controls. Centred 'jas·GRBL'
        with the connection status pill overlaid at the right."""
        bar = Gtk.Overlay()
        bar.get_style_context().add_class("jg-titlebar")

        title = Gtk.Label()
        title.set_markup('jas<span foreground="#5c6672">·</span>GRBL')
        title.get_style_context().add_class("jg-title")
        title.set_hexpand(True)
        title.set_halign(Gtk.Align.CENTER)
        title.set_margin_top(7)
        title.set_margin_bottom(7)
        bar.add(title)

        # Connection status pill, overlaid on the right.
        pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pill.get_style_context().add_class("jg-conn")
        pill.set_halign(Gtk.Align.END)
        pill.set_valign(Gtk.Align.CENTER)
        pill.set_margin_end(10)
        self._conn_dot = Gtk.Box()
        self._conn_dot.set_size_request(8, 8)
        self._conn_dot.get_style_context().add_class("jg-conn-dot")
        self._conn_dot.get_style_context().add_class("off")
        self._conn_label = Gtk.Label(label="Disconnected")
        pill.pack_start(self._conn_dot, False, False, 0)
        pill.pack_start(self._conn_label, False, False, 0)
        bar.add_overlay(pill)
        return bar

    def _update_conn_status(self) -> None:
        """Reflect the serial connection on the title-bar status pill."""
        if not hasattr(self, "_conn_dot"):
            return
        connected = self.serial.is_connected()
        ctx = self._conn_dot.get_style_context()
        ctx.remove_class("on")
        ctx.remove_class("off")
        ctx.add_class("on" if connected else "off")
        self._conn_label.set_text("Connected" if connected else "Disconnected")

    # ====================================================== Preview (left)
    def _build_preview_frame(self) -> Gtk.Widget:
        # No frame label/border: the preview canvas fills the whole left pane.
        overlay = Gtk.Overlay()
        self.preview = PreviewCanvas()
        overlay.add(self.preview)

        # Work area (Inkscape document size) shown read-only at the top of the preview.
        self.lbl_docsize = Gtk.Label()
        self.lbl_docsize.get_style_context().add_class("jg-docsize")
        self.lbl_docsize.set_halign(Gtk.Align.START)
        self.lbl_docsize.set_valign(Gtk.Align.START)
        self.lbl_docsize.set_margin_top(6)
        self.lbl_docsize.set_margin_start(6)
        self._update_docsize_label()
        overlay.add_overlay(self.lbl_docsize)

        # Status pills at the top-right (same row as Work area): machine profile
        # (green: GRBL / Vinyl) and output mode (blue: Engraving / Plotter).
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        status_box.set_halign(Gtk.Align.END)
        status_box.set_valign(Gtk.Align.START)
        status_box.set_margin_top(6)
        status_box.set_margin_end(6)
        self.lbl_status_profile = Gtk.Label()
        self.lbl_status_profile.get_style_context().add_class("jg-status-profile")
        self.lbl_status_mode = Gtk.Label()
        self.lbl_status_mode.get_style_context().add_class("jg-status-mode")
        status_box.pack_start(self.lbl_status_profile, False, False, 0)
        status_box.pack_start(self.lbl_status_mode, False, False, 0)
        self._update_status_labels()
        overlay.add_overlay(status_box)

        # Live X/Y readout at the bottom-right of the preview (design element). Tracks the
        # GRBL work position (status reports) or the vinyl head position.
        self.lbl_canvas_pos = Gtk.Label(label="X 0.0   Y 0.0")
        self.lbl_canvas_pos.get_style_context().add_class("jg-canvas-pos")
        self.lbl_canvas_pos.set_halign(Gtk.Align.END)
        self.lbl_canvas_pos.set_valign(Gtk.Align.END)
        self.lbl_canvas_pos.set_margin_bottom(8)
        self.lbl_canvas_pos.set_margin_end(8)
        overlay.add_overlay(self.lbl_canvas_pos)

        # Centered spinner overlay, reused for both "Generating G-code…" (generate) and
        # "Sending…" (streaming). The label text is set per use via _show_overlay().
        self._spin_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._spin_box.get_style_context().add_class("jg-spin")
        self._spin_box.set_halign(Gtk.Align.CENTER)
        self._spin_box.set_valign(Gtk.Align.CENTER)
        self._spinner = Gtk.Spinner()
        self._spinner.set_size_request(40, 40)
        self._spin_box.pack_start(self._spinner, False, False, 0)
        self._spin_label = Gtk.Label(label="Generating G-code…")
        self._spin_box.pack_start(self._spin_label, False, False, 0)
        self._spin_box.set_no_show_all(True)
        overlay.add_overlay(self._spin_box)

        return overlay

    def _show_overlay(self, text: str) -> None:
        """Show the centred spinner overlay on the preview with the given caption."""
        self._spin_label.set_text(text)
        # Clear no-show-all (set at build so the top-level show_all() skips it) so this
        # direct show_all() actually reveals the overlay.
        self._spin_box.set_no_show_all(False)
        self._spin_box.show_all()
        self._spinner.start()

    def _hide_overlay(self) -> None:
        self._spinner.stop()
        self._spin_box.hide()
        self._spin_box.set_no_show_all(True)

    def _set_overlay_progress(self, done: int, total: int) -> bool:
        """Update the 'Sending…' overlay with a live percentage. Live progress lives on the
        overlay (not the Serial Log), so a long job does not spam the log - the log only
        marks the start, the end, and any error. Safe as a GLib.idle_add callback."""
        if total > 0:
            self._spin_label.set_text("Sending… %d%%" % int(done * 100 / total))
        return False        # one-shot idle callback

    def _sync_preview_machine(self, *_):
        # Home is derived from the machine profile (GRBL -> Bottom-Left,
        # Vinyl Cutter -> Top-Right); it defines the whole coordinate system.
        self.preview.set_machine(self.doc_w_mm, self.doc_h_mm, self.config.home)

    # ====================================================== Right notebook
    def _build_right_notebook(self) -> Gtk.Notebook:
        nb = Gtk.Notebook()
        nb.set_size_request(390, -1)
        self.notebook = nb
        self._gcode_tab = self._build_gcode_tab()
        nb.append_page(self._gcode_tab, Gtk.Label(label="Generate"))

        # GRBL and VINYL machine tabs. Only the one matching the current machine
        # profile is enabled (clickable); the other is greyed out. VINYL is an empty
        # placeholder for now (prepared for a later step).
        self.tab_grbl = self._build_machine_tab()
        self.tab_grbl_label = Gtk.Label(label="GRBL")
        nb.append_page(self.tab_grbl, self.tab_grbl_label)

        self.tab_vinyl = self._build_vinyl_tab()
        self.tab_vinyl_label = Gtk.Label(label="VINYL")
        nb.append_page(self.tab_vinyl, self.tab_vinyl_label)

        # Error Log tab (always last, always visible): every error the extension hits.
        self.error_log = ErrorLogView()
        self.tab_error = self.error_log
        nb.append_page(self.tab_error, self._build_error_tab_label())
        self._err_tab_index = nb.page_num(self.tab_error)
        # Reset the unseen-error badge when the user opens the Error Log tab.
        nb.connect("switch-page", self._on_switch_page)

        self._update_machine_tabs()
        nb.set_current_page(0)          # always open on the Generate tab
        return nb

    def _build_error_tab_label(self) -> Gtk.Widget:
        """Tab label 'Error Log' plus a red badge showing the unseen-error count."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.pack_start(Gtk.Label(label="Error Log"), False, False, 0)
        self.err_badge = Gtk.Label()
        self.err_badge.get_style_context().add_class("jg-badge")
        self.err_badge.set_no_show_all(True)          # hidden until there is a count
        box.pack_start(self.err_badge, False, False, 0)
        box.show_all()
        return box

    def _on_switch_page(self, _nb, _page, page_num) -> None:
        if page_num == self._err_tab_index:
            self._err_unseen = 0
            self._update_error_badge()

    def _update_error_badge(self) -> None:
        if self._err_unseen > 0:
            self.err_badge.set_text(str(self._err_unseen))
            self.err_badge.set_no_show_all(False)
            self.err_badge.show()
        else:
            self.err_badge.hide()
            self.err_badge.set_no_show_all(True)

    def _error_log_add(self, actor: str, message: str) -> bool:
        """Record one error in the Error Log tab and bump the badge unless that tab is
        already showing. Safe to schedule via GLib.idle_add from worker threads."""
        if not hasattr(self, "error_log"):
            return False
        self.error_log.add(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), actor, message)
        if self.notebook.get_current_page() != self._err_tab_index:
            self._err_unseen += 1
            self._update_error_badge()
        return False        # so it can be used as a GLib.idle_add callback

    def _update_machine_tabs(self) -> None:
        """Show only the machine tab matching the current profile (GRBL vs Vinyl); hide
        the other. A GtkNotebook hides a page's tab when its child is not visible.
        `set_no_show_all` keeps a hidden tab hidden through the top-level show_all()."""
        grbl_on = (self.profile == C.PROFILE_GRBL)
        for child, on in ((self.tab_grbl, grbl_on), (self.tab_vinyl, not grbl_on)):
            child.set_no_show_all(not on)
            if on:
                child.show_all()
            else:
                child.hide()
        # Never leave a now-hidden machine tab showing: fall back to Generate (page 0).
        active = self.notebook.get_nth_page(self.notebook.get_current_page())
        if active in (self.tab_grbl, self.tab_vinyl) and not active.get_visible():
            self.notebook.set_current_page(0)

    # ------------------------------------------------------ GCode tab
    def _build_gcode_tab(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(8)

        # 1) Fill controls (top)
        fill_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        fill_box.pack_start(Gtk.Label(label="Fill"), False, False, 0)
        self.c_fill = Gtk.ComboBoxText()
        for ftype in C.FILL_TYPES:
            self.c_fill.append(ftype, C.FILL_LABELS[ftype])
        self.c_fill.set_active_id(self.config.fill_type)
        fill_box.pack_start(self.c_fill, False, False, 0)
        fill_box.pack_start(Gtk.Label(label="Angle"), False, False, 0)
        self.s_angle = Stepper(self.config.fill_angle, 0, 180, 5, digits=0, width_chars=4)
        fill_box.pack_start(self.s_angle, False, False, 0)
        fill_box.pack_start(Gtk.Label(label="Spacing"), False, False, 0)
        self.s_spacing = Stepper(self.config.fill_spacing, 0.05, 10.0, 0.05,
                                 digits=2, width_chars=5)
        fill_box.pack_start(self.s_spacing, False, False, 0)
        self.chk_shade = Gtk.CheckButton(label="Shade by color")
        self.chk_shade.set_active(bool(self.config.shade_density))
        self.chk_shade.set_tooltip_text(
            "Fill density follows the fill colour: darker engraves denser, "
            "lighter sparser. Spacing above is the spacing for a black fill.")
        fill_box.pack_start(self.chk_shade, False, False, 0)
        box.pack_start(fill_box, False, False, 0)

        # 2) Layer table as a bordered card, top-aligned, with an info line beneath it
        #    (matches the design: compact card at the top, empty space below).
        self.layer_table = LayerTable()
        table_card = Gtk.Frame()
        table_card.add(self.layer_table)

        info = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        info.pack_start(
            Gtk.Image.new_from_icon_name("dialog-information-symbolic", Gtk.IconSize.MENU),
            False, False, 0)
        info_lbl = Gtk.Label(
            label="Layers are imported from your Inkscape document. "
                  "Adjust power, speed & passes per layer.")
        info_lbl.set_xalign(0.0)
        info_lbl.set_line_wrap(True)
        info_lbl.get_style_context().add_class("jg-pos")
        info.pack_start(info_lbl, True, True, 0)

        table_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        table_wrap.set_valign(Gtk.Align.START)
        table_wrap.pack_start(table_card, False, False, 0)
        table_wrap.pack_start(info, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.add(table_wrap)
        box.pack_start(scrolled, True, True, 0)

        # 3) Single action row, left to right: machine profile (GRBL | Vinyl Cutter),
        #    output mode (Engraving | Plotter), Generate, Clear, Save.
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        # Machine profile selector (GRBL | Vinyl Cutter): fixes the Home corner
        # (GRBL -> Bottom-Left, Vinyl -> Top-Right) and the export format.
        prof_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        prof_box.get_style_context().add_class("linked")
        prof_box.get_style_context().add_class("jg-toggle-group")
        self.rb_grbl = Gtk.RadioButton.new_with_label_from_widget(
            None, C.PROFILE_LABELS[C.PROFILE_GRBL])
        self.rb_grbl.set_mode(False)
        self.rb_vinyl = Gtk.RadioButton.new_with_label_from_widget(
            self.rb_grbl, C.PROFILE_LABELS[C.PROFILE_VINYL])
        self.rb_vinyl.set_mode(False)
        for rb in (self.rb_grbl, self.rb_vinyl):
            rb.get_style_context().add_class("jg-profile")
        (self.rb_vinyl if self.profile == C.PROFILE_VINYL else self.rb_grbl).set_active(True)
        self.rb_grbl.connect("toggled", self._on_profile_changed)
        self.rb_vinyl.connect("toggled", self._on_profile_changed)
        prof_box.pack_start(self.rb_grbl, False, False, 0)
        prof_box.pack_start(self.rb_vinyl, False, False, 0)
        row_box.pack_start(prof_box, False, False, 0)

        # Output mode selector (Engraving | Plotter): drives the per-layer columns.
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        mode_box.get_style_context().add_class("linked")
        mode_box.get_style_context().add_class("jg-toggle-group")
        self.rb_engrave = Gtk.RadioButton.new_with_label_from_widget(
            None, C.MODE_LABELS[C.MODE_ENGRAVING])
        self.rb_engrave.set_mode(False)                 # render as a toggle button
        self.rb_plotter = Gtk.RadioButton.new_with_label_from_widget(
            self.rb_engrave, C.MODE_LABELS[C.MODE_PLOTTER])
        self.rb_plotter.set_mode(False)
        for rb in (self.rb_engrave, self.rb_plotter):
            rb.get_style_context().add_class("jg-mode")
        (self.rb_plotter if self.mode == C.MODE_PLOTTER else self.rb_engrave).set_active(True)
        self.rb_engrave.connect("toggled", self._on_mode_changed)
        self.rb_plotter.connect("toggled", self._on_mode_changed)
        mode_box.pack_start(self.rb_engrave, False, False, 0)
        mode_box.pack_start(self.rb_plotter, False, False, 0)
        row_box.pack_start(mode_box, False, False, 0)

        # Generate (green) takes the remaining width; Clear and Save stay compact.
        self.btn_generate = Gtk.Button(label="Generate G-code")
        self.btn_generate.get_style_context().add_class("jg-generate")
        self.btn_generate.connect("clicked", self._on_generate)
        self.btn_clear = Gtk.Button.new_from_icon_name("edit-clear", Gtk.IconSize.BUTTON)
        self.btn_clear.set_tooltip_text("Clear preview and temporary code files")
        self.btn_clear.connect("clicked", self._on_clear)
        # Single Save button; its target format follows the machine profile
        # (GRBL -> G-code, Vinyl Cutter -> HPGL). Floppy-disk icon.
        self.btn_export = self._icon_text_button("media-floppy", "Save")
        self.btn_export.connect("clicked", self._on_export)
        row_box.pack_start(self.btn_generate, True, True, 0)
        row_box.pack_start(self.btn_clear, False, False, 0)
        row_box.pack_start(self.btn_export, False, False, 0)

        # Footer divider above the action bar (design's border-top).
        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        box.pack_start(row_box, False, False, 0)
        self._update_export_tooltip()
        return box

    @staticmethod
    def _icon_text_button(icon_name: str, text: str) -> Gtk.Button:
        btn = Gtk.Button()
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        inner.pack_start(
            Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON), False, False, 0)
        inner.pack_start(Gtk.Label(label=text), False, False, 0)
        btn.add(inner)
        return btn

    # ------------------------------------------------------ Machine tab
    def _build_machine_tab(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(8)
        # Connect and Jog sit side by side on one row pinned to the top; each frame
        # hugs its own content height (valign START) so neither stretches to match the
        # taller one. The Serial Log then fills all remaining height below.
        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top_row.set_valign(Gtk.Align.START)
        top_row.set_vexpand(False)                        # never grow into the log's space
        # FILL (not START): both frames stretch to the row's height so Connect and Jog
        # render at equal height side by side.
        self.connect_frame = self._build_connect_frame()
        connect = self.connect_frame
        connect.set_valign(Gtk.Align.FILL)
        self.jog_frame = self._build_jog_frame()
        self.jog_frame.set_valign(Gtk.Align.FILL)
        top_row.pack_start(connect, True, True, 0)
        top_row.pack_start(self.jog_frame, True, True, 0)
        box.pack_start(top_row, False, False, 0)
        box.pack_start(self._build_serial_frame(), True, True, 0)
        return box

    def _build_connect_frame(self) -> Gtk.Frame:
        frame = Gtk.Frame(label="CONNECT")
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        grid.set_border_width(8)
        frame.add(grid)

        def add(label, widget, r):
            lab = Gtk.Label(label=label)
            lab.set_xalign(0.0)
            grid.attach(lab, 0, r, 1, 1)
            grid.attach(widget, 1, r, 1, 1)

        port_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.c_port = Gtk.ComboBoxText.new_with_entry()
        self.c_port.set_hexpand(True)
        btn_refresh = Gtk.Button.new_from_icon_name("view-refresh", Gtk.IconSize.BUTTON)
        btn_refresh.set_tooltip_text("Refresh serial ports")
        btn_refresh.connect("clicked", lambda *_: self._refresh_ports())
        port_box.pack_start(self.c_port, True, True, 0)
        port_box.pack_start(btn_refresh, False, False, 0)
        add("USB Port", port_box, 0)

        self.c_baud = Gtk.ComboBoxText.new_with_entry()
        for b in C.BAUD_RATES:
            self.c_baud.append_text(str(b))
        self.c_baud.get_child().set_text(str(self.config.baud))
        add("Baud Rate", self.c_baud, 1)

        self.btn_connect = Gtk.Button(label="Connect")
        self.btn_connect.connect("clicked", self._on_connect_clicked)
        grid.attach(self.btn_connect, 0, 2, 2, 1)

        # Grbl Setting: dump the controller's settings ($$). Only usable once connected.
        self.btn_grbl_setting = Gtk.Button(label="Grbl Setting")
        self.btn_grbl_setting.set_tooltip_text("Query GRBL settings ($$)")
        self.btn_grbl_setting.connect("clicked", self._on_grbl_setting)
        grid.attach(self.btn_grbl_setting, 0, 3, 2, 1)
        return frame

    # ------------------------------------------------------ Jog frame
    def _build_jog_frame(self) -> Gtk.Frame:
        frame = Gtk.Frame(label="JOG")
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_border_width(8)
        frame.add(outer)

        # Card header row: live work-position readout (true X/Y from GRBL status
        # reports), right-aligned like the design's JOG header.
        self.lbl_grbl_pos = Gtk.Label(label="Pos: —")
        self.lbl_grbl_pos.set_xalign(1.0)
        self.lbl_grbl_pos.set_halign(Gtk.Align.END)
        self.lbl_grbl_pos.get_style_context().add_class("jg-pos")
        outer.pack_start(self.lbl_grbl_pos, False, False, 0)

        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)

        # Left: directional pad. +Y / -X Home +X / -Y around a 3x3 grid.
        pad = Gtk.Grid(column_spacing=6, row_spacing=6)
        pad.set_valign(Gtk.Align.START)
        pad.attach(self._jog_button("+Y", 0, 1), 1, 0, 1, 1)
        pad.attach(self._jog_button("-X", -1, 0), 0, 1, 1, 1)
        btn_home = Gtk.Button.new_from_icon_name("go-home-symbolic", Gtk.IconSize.BUTTON)
        btn_home.get_style_context().add_class("jg-jog-home")
        btn_home.set_tooltip_text("Home ($H)")
        btn_home.connect("clicked", self._on_home)
        pad.attach(btn_home, 1, 1, 1, 1)
        pad.attach(self._jog_button("+X", 1, 0), 2, 1, 1, 1)
        pad.attach(self._jog_button("-Y", 0, -1), 1, 2, 1, 1)
        row1.pack_start(pad, False, False, 0)

        # Right column: Jog Feed readout, Step stepper, Continuous, Set Home.
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        feed_lbl = Gtk.Label(label="Jog Feed")
        feed_lbl.set_xalign(0.0)
        feed_lbl.get_style_context().add_class("jg-pos")
        col.pack_start(feed_lbl, False, False, 0)
        # Big static readout (design): a fixed jog feed for this machine (mm/min).
        self.adj_jog_feed = Gtk.Adjustment(
            value=3000, lower=500, upper=5000, step_increment=100, page_increment=500)
        feed_val = Gtk.Label(label="%d" % int(self.adj_jog_feed.get_value()))
        feed_val.set_xalign(0.0)
        feed_val.get_style_context().add_class("jg-jogfeed")
        col.pack_start(feed_val, False, False, 0)

        step_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        step_lbl = Gtk.Label(label="Step")
        step_lbl.set_xalign(0.0)
        step_row.pack_start(step_lbl, False, False, 0)
        step = Stepper(10, 0.1, 1000, 1, digits=1, width_chars=5)
        self.adj_jog_step = step.adjustment
        self.spin_jog_step = step
        step_row.pack_start(step, True, True, 0)
        step_row.pack_start(Gtk.Label(label="mm"), False, False, 0)
        col.pack_start(step_row, False, False, 0)

        # Continuous: hold-to-jog until release; the fixed step no longer applies.
        self.chk_jog_continuous = Gtk.CheckButton(label="Continuous")
        self.chk_jog_continuous.connect(
            "toggled", lambda b: self.spin_jog_step.set_sensitive(not b.get_active()))
        col.pack_start(self.chk_jog_continuous, False, False, 0)

        btn_set_home = Gtk.Button(label="Set Home")
        btn_set_home.set_tooltip_text("Set current position as work origin (G10 L20 P1 X0 Y0)")
        btn_set_home.connect("clicked", self._on_set_home)
        col.pack_start(btn_set_home, False, False, 0)
        row1.pack_start(col, True, True, 0)
        outer.pack_start(row1, True, True, 0)

        # Row 2: icon-only Start (green, -> red Stop while streaming), Frame, Test Laser.
        # Wide green Start (flex) + compact Pause / Frame / Test (design).
        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.btn_start = Gtk.Button()
        self.btn_start.set_always_show_image(True)
        self.btn_start.get_style_context().add_class("jg-start")
        self.btn_start.connect("clicked", self._on_send_to_machine)
        self._set_start_icon("media-playback-start-symbolic")

        # Pause/Resume: live only while a job streams (feed-hold '!' / cycle-start '~').
        # Sensitivity + icon are managed in _update_action_buttons; kept OUT of the
        # stream-lock list so it stays clickable during a stream.
        self.btn_pause = Gtk.Button()
        self.btn_pause.set_always_show_image(True)
        self.btn_pause.connect("clicked", self._on_pause_resume)
        self._set_pause_icon("media-playback-pause-symbolic")
        self.btn_pause.set_tooltip_text("Pause the running job")
        self.btn_pause.set_sensitive(False)

        self.btn_frame_grbl = Gtk.Button.new_from_icon_name(
            "view-fullscreen-symbolic", Gtk.IconSize.BUTTON)
        self.btn_frame_grbl.set_tooltip_text("Frame: trace the job's bounding box (laser off)")
        self.btn_frame_grbl.connect("clicked", self._on_frame)

        self.btn_test_laser = Gtk.Button(label="\U0001F525")   # fire emoji
        self.btn_test_laser.get_style_context().add_class("jg-test")
        self.btn_test_laser.set_tooltip_text("Test Laser: brief low-power pulse")
        self.btn_test_laser.connect("clicked", self._on_test_laser)

        row2.pack_start(self.btn_start, True, True, 0)
        row2.pack_start(self.btn_pause, False, False, 0)
        row2.pack_start(self.btn_frame_grbl, False, False, 0)
        row2.pack_start(self.btn_test_laser, False, False, 0)
        outer.pack_start(row2, False, False, 0)

        # GRBL jog controls that must lock during a stream (everything but Start/Stop).
        self._grbl_lock_widgets = [pad, col, self.btn_frame_grbl, self.btn_test_laser]
        return frame

    def _jog_button(self, label: str, dx: int, dy: int) -> Gtk.Button:
        """A directional jog button: click = one step; press+hold = continuous
        jog while Continuous is checked (cancelled on release)."""
        btn = Gtk.Button(label=label)
        btn.get_style_context().add_class("jg-jog")
        # "clicked" only acts in step mode; "pressed"/"released" only in continuous
        # mode - so the two never both fire for one interaction.
        btn.connect("clicked", lambda *_: self._on_jog_step(dx, dy))
        btn.connect("pressed", lambda *_: self._on_jog_continuous_start(dx, dy))
        btn.connect("released", lambda *_: self._on_jog_continuous_stop())
        return btn

    def _set_start_icon(self, icon_name: str) -> None:
        self.btn_start.set_image(
            Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON))

    def _set_pause_icon(self, icon_name: str) -> None:
        self.btn_pause.set_image(
            Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON))

    def _update_docsize_label(self) -> None:
        self.lbl_docsize.set_text(
            "Work area: %.1f × %.1f mm (Inkscape document)" % (self.doc_w_mm, self.doc_h_mm))

    def _update_status_labels(self) -> None:
        """Refresh the preview status pills to match the current profile + mode.

        Colours follow the design: GRBL green / Vinyl cyan; Engraving blue / Plotter
        purple (the variant class is toggled; the base pill class stays)."""
        is_vinyl = self.profile == C.PROFILE_VINYL
        self.lbl_status_profile.set_text("VINYL" if is_vinyl else "GRBL")
        ctx = self.lbl_status_profile.get_style_context()
        (ctx.add_class if is_vinyl else ctx.remove_class)("jg-status-vinyl")

        is_plotter = self.mode == C.MODE_PLOTTER
        self.lbl_status_mode.set_text(C.MODE_LABELS[self.mode])
        ctx2 = self.lbl_status_mode.get_style_context()
        (ctx2.add_class if is_plotter else ctx2.remove_class)("jg-status-plotter")

    def _build_serial_frame(self) -> Gtk.Frame:
        frame = Gtk.Frame(label="SERIAL LOG")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(8)
        frame.add(box)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.chk_autoscroll = Gtk.CheckButton(label="Autoscroll")
        self.chk_autoscroll.set_active(True)
        self.chk_autoscroll.connect(
            "toggled", lambda b: setattr(self.log_view, "autoscroll", b.get_active()))
        btn_clear_log = Gtk.Button(label="Clear log")
        btn_clear_log.connect("clicked", lambda *_: self.log_view.clear())
        toolbar.pack_start(self.chk_autoscroll, False, False, 0)
        toolbar.pack_end(btn_clear_log, False, False, 0)
        box.pack_start(toolbar, False, False, 0)

        self.log_view = LogView()
        box.pack_start(self.log_view, True, True, 0)

        send_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.e_command = Gtk.Entry()
        self.e_command.set_placeholder_text("GRBL command (e.g. $$, $H, G0 X0 Y0)")
        self.e_command.connect("activate", self._on_send)
        self.btn_cmd_send = Gtk.Button(label="Send")
        self.btn_cmd_send.connect("clicked", self._on_send)
        send_box.pack_start(self.e_command, True, True, 0)
        send_box.pack_start(self.btn_cmd_send, False, False, 0)
        box.pack_start(send_box, False, False, 0)

        if not serial_io.pyserial_available():
            self.log_view.append("WARN", "pyserial not installed - serial features disabled. "
                                          "Install with: pip install pyserial")
        return frame

    # ====================================================== VINYL tab
    def _build_vinyl_tab(self) -> Gtk.Box:
        """Vinyl-cutter machine tab. Same layout as the GRBL tab (Connect + Jog on the
        top row) but the Serial Log is replaced by a Cut Setting frame, and the controls
        are tuned for a knife cutter (see the module docstring)."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(8)
        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top_row.set_valign(Gtk.Align.START)
        top_row.set_vexpand(False)
        self.v_connect_frame = self._build_vinyl_connect_frame()
        connect = self.v_connect_frame
        connect.set_valign(Gtk.Align.FILL)
        self.vinyl_jog_frame = self._build_vinyl_jog_frame()
        self.vinyl_jog_frame.set_valign(Gtk.Align.FILL)
        top_row.pack_start(connect, True, True, 0)
        top_row.pack_start(self.vinyl_jog_frame, True, True, 0)
        box.pack_start(top_row, False, False, 0)
        # Row 2 (where the Serial Log lives on the GRBL tab): the Cut Setting frame.
        box.pack_start(self._build_cut_setting_frame(), False, False, 0)
        return box

    def _build_vinyl_connect_frame(self) -> Gtk.Frame:
        frame = Gtk.Frame(label="CONNECT")
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        grid.set_border_width(8)
        frame.add(grid)

        def add(label, widget, r):
            lab = Gtk.Label(label=label)
            lab.set_xalign(0.0)
            grid.attach(lab, 0, r, 1, 1)
            grid.attach(widget, 1, r, 1, 1)

        port_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.v_c_port = Gtk.ComboBoxText.new_with_entry()
        self.v_c_port.set_hexpand(True)
        btn_refresh = Gtk.Button.new_from_icon_name("view-refresh", Gtk.IconSize.BUTTON)
        btn_refresh.set_tooltip_text("Refresh USB ports")
        btn_refresh.connect("clicked", lambda *_: self._refresh_ports())
        port_box.pack_start(self.v_c_port, True, True, 0)
        port_box.pack_start(btn_refresh, False, False, 0)
        add("USB Port", port_box, 0)

        self.v_c_baud = Gtk.ComboBoxText.new_with_entry()
        for b in C.BAUD_RATES:
            self.v_c_baud.append_text(str(b))
        # Vinyl cutters default to 9600 (kept separate from the GRBL baud).
        self.v_c_baud.get_child().set_text(str(self.config.vinyl_baud))
        add("Baud Rate", self.v_c_baud, 1)

        # Flow control: how the cutter throttles the host (it sends no per-line ACK).
        self.v_c_flow = Gtk.ComboBoxText()
        for fc in C.FLOW_CONTROLS:
            self.v_c_flow.append(fc, C.FLOW_LABELS[fc])
        self.v_c_flow.set_active_id(
            self.config.flow_control if self.config.flow_control in C.FLOW_CONTROLS
            else C.DEFAULT_FLOW)
        self.v_c_flow.set_tooltip_text(
            "How the cutter paces the data stream. Software (XON/XOFF) suits most HPGL "
            "cutters; Hardware (RTS/CTS) needs wired flow lines. Applied on next Connect.")
        add("Flow Control", self.v_c_flow, 2)

        self.v_btn_connect = Gtk.Button(label="Connect")
        self.v_btn_connect.connect("clicked", self._on_connect_clicked)
        grid.attach(self.v_btn_connect, 0, 3, 2, 1)

        # Reset Machine: re-initialise the cutter (ESC.R device reset). Replaces the
        # GRBL tab's "Grbl Setting" button. Only usable once connected.
        self.v_btn_reset = Gtk.Button(label="Reset Machine")
        self.v_btn_reset.set_tooltip_text("Reset the cutter (ESC.R + HPGL IN)")
        self.v_btn_reset.connect("clicked", self._v_on_reset)
        grid.attach(self.v_btn_reset, 0, 4, 2, 1)

        # Advanced: send pacing (chunk size + inter-chunk delay). Matters mainly when
        # Flow Control is None or a cutter's XON/XOFF is unreliable.
        adv = Gtk.Expander(label="Advanced")
        adv_grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        adv_grid.set_margin_top(6)
        lbl_chunk = Gtk.Label(label="Chunk (bytes)")
        lbl_chunk.set_xalign(0.0)
        adv_grid.attach(lbl_chunk, 0, 0, 1, 1)
        self.v_spin_send_chunk = Stepper(
            self.config.send_chunk, 1, 8192, 64, digits=0, width_chars=6)
        self.v_adj_send_chunk = self.v_spin_send_chunk.adjustment
        self.v_spin_send_chunk.set_hexpand(True)
        adv_grid.attach(self.v_spin_send_chunk, 1, 0, 1, 1)
        lbl_delay = Gtk.Label(label="Delay (ms)")
        lbl_delay.set_xalign(0.0)
        adv_grid.attach(lbl_delay, 0, 1, 1, 1)
        self.v_spin_send_delay = Stepper(
            self.config.send_delay_ms, 0, 1000, 5, digits=0, width_chars=6)
        self.v_adj_send_delay = self.v_spin_send_delay.adjustment
        self.v_spin_send_delay.set_hexpand(True)
        adv_grid.attach(self.v_spin_send_delay, 1, 1, 1, 1)
        adv.add(adv_grid)
        grid.attach(adv, 0, 5, 2, 1)
        return frame

    def _build_vinyl_jog_frame(self) -> Gtk.Frame:
        frame = Gtk.Frame(label="JOG")
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_border_width(8)
        frame.add(outer)

        # Card header: head-position readout (relative to the work origin), right-aligned
        # like the design's JOG header. Set Origin resets it to 0,0 - visible proof the
        # origin moved even though the head itself does not move on click.
        self.v_lbl_pos = Gtk.Label(label="X 0.0   Y 0.0 mm")
        self.v_lbl_pos.set_xalign(1.0)
        self.v_lbl_pos.set_halign(Gtk.Align.END)
        self.v_lbl_pos.get_style_context().add_class("jg-pos")
        outer.pack_start(self.v_lbl_pos, False, False, 0)

        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)

        # Left: directional pad. The cutter's HPGL axes are transposed vs the physical
        # carriage (HPGL X drives the Y motor and vice versa) and +X runs leftward, so each
        # button is pre-mapped to the HPGL delta that moves the head in its labelled physical
        # direction - matching the proven reference: +Y->(-1,0), +X->(0,1), -X->(0,-1),
        # -Y->(1,0). (_v_jog applies no further swap for jog.)
        pad = Gtk.Grid(column_spacing=6, row_spacing=6)
        pad.set_valign(Gtk.Align.START)
        pad.attach(self._v_jog_button("+Y", -1, 0), 1, 0, 1, 1)
        pad.attach(self._v_jog_button("+X", 0, 1), 0, 1, 1, 1)
        btn_home = Gtk.Button.new_from_icon_name("go-home-symbolic", Gtk.IconSize.BUTTON)
        btn_home.get_style_context().add_class("jg-jog-home")
        btn_home.set_tooltip_text("Go to origin (PU0,0)")
        btn_home.connect("clicked", self._v_on_home)
        pad.attach(btn_home, 1, 1, 1, 1)
        pad.attach(self._v_jog_button("-X", 0, -1), 2, 1, 1, 1)
        pad.attach(self._v_jog_button("-Y", 1, 0), 1, 2, 1, 1)
        row1.pack_start(pad, False, False, 0)

        # Right column: Jog Feed (mm/s) + Step steppers + Set Origin (connection-gated),
        # then Clear Print Job (NOT gated - a stuck job must be clearable while offline).
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        col3_gated = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        feed_lbl = Gtk.Label(label="Jog Feed")
        feed_lbl.set_xalign(0.0)
        feed_lbl.get_style_context().add_class("jg-pos")
        col3_gated.pack_start(feed_lbl, False, False, 0)
        # Big static readout (design): a fixed jog feed for the cutter (mm/s).
        self.v_adj_jog_feed = Gtk.Adjustment(
            value=500, lower=100, upper=1000, step_increment=50, page_increment=100)
        v_feed_val = Gtk.Label(label="%d" % int(self.v_adj_jog_feed.get_value()))
        v_feed_val.set_xalign(0.0)
        v_feed_val.get_style_context().add_class("jg-jogfeed")
        col3_gated.pack_start(v_feed_val, False, False, 0)

        step_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        step_lbl = Gtk.Label(label="Step")
        step_lbl.set_xalign(0.0)
        step_row.pack_start(step_lbl, False, False, 0)
        v_step = Stepper(10, 0.1, 1000, 1, digits=1, width_chars=5)
        self.v_adj_jog_step = v_step.adjustment
        self.v_spin_jog_step = v_step
        step_row.pack_start(v_step, True, True, 0)
        step_row.pack_start(Gtk.Label(label="mm"), False, False, 0)
        col3_gated.pack_start(step_row, False, False, 0)

        btn_set_origin = Gtk.Button(label="Set Origin")
        btn_set_origin.set_tooltip_text(
            "Make the current head position the work (0,0); the readout resets and the cut "
            "will start here. The head does not move now.")
        btn_set_origin.connect("clicked", self._v_on_set_origin)
        col3_gated.pack_start(btn_set_origin, False, False, 0)
        col.pack_start(col3_gated, False, False, 0)

        # Clear Print Job: force-restart the whole Print Spooler to flush a wedged job.
        # Always enabled - a job wedged "forever" often happens while disconnected.
        self.v_btn_clear_job = Gtk.Button(label="Clear Print Job")
        self.v_btn_clear_job.set_tooltip_text(
            "Force-restart the Windows Print Spooler to flush stuck jobs (prompts for Admin)")
        self.v_btn_clear_job.connect("clicked", self._v_on_clear_print_job)
        col.pack_start(self.v_btn_clear_job, False, False, 0)
        row1.pack_start(col, True, True, 0)
        outer.pack_start(row1, True, True, 0)

        # Row 2: wide Start (flex) + compact Frame / Test Cut (design).
        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.v_btn_start = Gtk.Button()
        self.v_btn_start.set_always_show_image(True)
        self.v_btn_start.get_style_context().add_class("jg-start")
        self.v_btn_start.set_image(
            Gtk.Image.new_from_icon_name("media-playback-start-symbolic", Gtk.IconSize.BUTTON))
        self.v_btn_start.set_tooltip_text("Send the generated HPGL cut to the machine")
        self.v_btn_start.connect("clicked", self._v_on_send)

        self.v_btn_frame = Gtk.Button.new_from_icon_name(
            "view-fullscreen-symbolic", Gtk.IconSize.BUTTON)
        self.v_btn_frame.set_tooltip_text("Frame: trace the job's bounding box (pen up)")
        self.v_btn_frame.connect("clicked", self._v_on_frame)

        self.v_btn_test = Gtk.Button(label="Test Cut")
        self.v_btn_test.get_style_context().add_class("jg-test")
        self.v_btn_test.set_tooltip_text("Test Cut: cut a small 10 mm square with the Cut Setting")
        self.v_btn_test.connect("clicked", self._v_on_test_cut)

        row2.pack_start(self.v_btn_start, True, True, 0)
        row2.pack_start(self.v_btn_frame, False, False, 0)
        row2.pack_start(self.v_btn_test, False, False, 0)
        outer.pack_start(row2, False, False, 0)

        # Widgets that require an open connection. Toggled per-widget (not on the whole
        # frame) so Clear Print Job stays usable offline and the Start/Stop button stays
        # live during a stream (Frame/Test are listed individually, not via row2).
        self._v_conn_widgets = [pad, col3_gated, self.v_btn_frame, self.v_btn_test]
        return frame

    def _v_jog_button(self, label: str, dx: int, dy: int) -> Gtk.Button:
        btn = Gtk.Button(label=label)
        btn.get_style_context().add_class("jg-jog")
        btn.connect("clicked", lambda *_: self._v_jog(dx, dy))
        return btn

    def _build_cut_setting_frame(self) -> Gtk.Frame:
        """Cut Setting: knife Speed (mm/s) and Force (g). Sits where the GRBL tab keeps
        its Serial Log. Defaults match the plotter defaults (250 mm/s, 80 g)."""
        frame = Gtk.Frame(label="CUT SETTING")
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        grid.set_border_width(8)
        frame.add(grid)

        grid.attach(_left_label("Speed"), 0, 0, 1, 1)
        self.v_spin_cut_speed = Stepper(
            self.config.cut_speed, 1, 1000, 5, digits=0, width_chars=6)
        self.v_adj_cut_speed = self.v_spin_cut_speed.adjustment
        self.v_spin_cut_speed.set_hexpand(True)
        grid.attach(self.v_spin_cut_speed, 1, 0, 1, 1)
        grid.attach(_left_label("mm/s"), 2, 0, 1, 1)

        grid.attach(_left_label("Force"), 0, 1, 1, 1)
        self.v_spin_cut_force = Stepper(
            self.config.cut_force, 1, 500, 5, digits=0, width_chars=6)
        self.v_adj_cut_force = self.v_spin_cut_force.adjustment
        self.v_spin_cut_force.set_hexpand(True)
        grid.attach(self.v_spin_cut_force, 1, 1, 1, 1)
        grid.attach(_left_label("g"), 2, 1, 1, 1)
        return frame

    # ------------------------------------------------------ VINYL handlers
    def _v_cut_params(self):
        """(velocity cm/s, force g) from the Cut Setting, matching emit_hpgl's mapping."""
        speed = int(self.v_adj_cut_speed.get_value())          # mm/s
        force = max(1, int(self.v_adj_cut_force.get_value()))  # grams
        vs = max(1, int(round(speed / 10.0)))                  # mm/s -> cm/s (HPGL VS)
        return vs, force

    def _v_on_reset(self, *_):
        if not self.serial.is_connected():
            self.log_view.append("WARN", "connect to the machine first")
            return
        # ESC.R resets the RS-232 interface; IN re-initialises HPGL state (pen up,
        # defaults, origin at the current pen position). Sent as one payload so a spooled
        # printer receives a single RAW job rather than two. Tune for the LH721 if needed.
        self.serial.send_raw(b"\x1b.RIN;\n", "reset (ESC.R + IN)")
        self.log_view.append("INFO", "machine reset")

    def _v_swap_xy(self) -> bool:
        """Vinyl cutters transpose HPGL X/Y vs the physical carriage (HPGL X drives the feed,
        HPGL Y drives the pen carriage). The design frame is X=width, Y=height with Home at
        top-left; emitting to the cutter therefore always swaps (x,y)->(y,x). Fixed True (no
        toggle) - it is a property of this cutter class, matching the proven reference."""
        return True

    def _v_jog(self, dx: int, dy: int) -> None:
        if not self._jog_ready():
            return
        step_mm = float(self.v_adj_jog_step.get_value())
        # The button (dx, dy) is already the HPGL delta for the labelled physical direction
        # (see the pad in _build_vinyl_jog_frame), so no axis swap is applied here. Track the
        # head in software and emit an ABSOLUTE pen-up move: cutters like the LH721 ignore the
        # relative PR command (why jog appeared dead / confused), but honour absolute PU.
        self._v_head_mm[0] += dx * step_mm
        self._v_head_mm[1] += dy * step_mm
        u = C.HPGL_UNITS_PER_MM
        ax = int(round((self._v_head_mm[0] + self._v_origin_mm[0]) * u))
        ay = int(round((self._v_head_mm[1] + self._v_origin_mm[1]) * u))
        vs = max(1, int(self.v_adj_jog_feed.get_value() / 10.0))   # mm/s -> cm/s
        self.serial.send_line("VS%d;PU%d,%d;" % (vs, ax, ay))
        self._v_update_pos_label()

    def _v_origin_units(self) -> Tuple[int, int]:
        """The Set-Origin offset in HPGL units, added to every absolute coordinate we send
        (jog, home, frame, test, cut) so work (0,0) sits where the head was jogged."""
        u = C.HPGL_UNITS_PER_MM
        return int(round(self._v_origin_mm[0] * u)), int(round(self._v_origin_mm[1] * u))

    def _v_update_pos_label(self) -> None:
        if hasattr(self, "v_lbl_pos"):
            self.v_lbl_pos.set_text(
                "X %.1f   Y %.1f mm" % (self._v_head_mm[0], self._v_head_mm[1]))
        if hasattr(self, "lbl_canvas_pos"):
            self.lbl_canvas_pos.set_text(
                "X %.1f   Y %.1f" % (self._v_head_mm[0], self._v_head_mm[1]))

    def _v_apply_origin_hpgl(self, data: bytes) -> bytes:
        """Shift every absolute coordinate pair in a generated HPGL blob by the Set-Origin
        offset so the cut lands at the work origin. No-op when the origin is (0,0). Only
        PU/PD/PA coordinates are shifted; IN/VS/FS/SP and bare 'PU;' pass through unchanged."""
        ox, oy = self._v_origin_units()
        if ox == 0 and oy == 0:
            return data
        import re
        cmd_re = re.compile(r"^\s*(P[UDA])\s*([-0-9,\s]*?)\s*;?\s*$", re.IGNORECASE)
        out = []
        for line in data.decode("latin-1").splitlines():
            m = cmd_re.match(line)
            nums = re.split(r"[,\s]+", m.group(2).strip()) if (m and m.group(2).strip()) else []
            if not m or not nums or len(nums) % 2 != 0:
                out.append(line)
                continue
            try:
                shifted = []
                for i in range(0, len(nums), 2):
                    shifted.append(str(int(nums[i]) + ox))
                    shifted.append(str(int(nums[i + 1]) + oy))
            except ValueError:
                out.append(line)
                continue
            out.append("%s%s;" % (m.group(1).upper(), ",".join(shifted)))
        return ("\n".join(out) + "\n").encode("latin-1")

    def _v_on_home(self, *_):
        if not self._jog_ready():
            return
        # Absolute move back to the work origin; reset the tracked head to (0,0).
        self._v_head_mm = [0.0, 0.0]
        ox, oy = self._v_origin_units()
        self.serial.send_line("PU%d,%d;" % (ox, oy))
        self._v_update_pos_label()

    def _v_on_set_origin(self, *_):
        if not self._jog_ready():
            return
        # Software origin, EXACTLY like the reference's setOriginHere: the head's current
        # machine position becomes the new work (0,0). No command is sent to the cutter - the
        # head does not move now; instead every coordinate we emit from here (jog, Home,
        # Frame, Test Cut, the cut) is shifted by this offset, so the cut starts here.
        self._v_origin_mm[0] += self._v_head_mm[0]
        self._v_origin_mm[1] += self._v_head_mm[1]
        self._v_head_mm = [0.0, 0.0]
        self._v_update_pos_label()
        self.log_view.append(
            "INFO", "origin set at machine X=%.1f Y=%.1f mm"
            % (self._v_origin_mm[0], self._v_origin_mm[1]))

    def _v_on_frame(self, *_):
        if not self._jog_ready():
            return
        if self.program is None or not self.program.segments:
            self.log_view.append("WARN", "generate the cut first to frame the job")
            return
        pts = []
        for _kind, a, b in self.program.segments:
            pts.append(a)
            pts.append(b)
        xmin, ymin, xmax, ymax = G.bbox(pts)
        u = C.HPGL_UNITS_PER_MM
        corners = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax), (xmin, ymin)]
        swap = self._v_swap_xy()
        ox, oy = self._v_origin_units()

        def coord(x, y):
            xu, yu = int(round(x * u)), int(round(y * u))
            if swap:
                xu, yu = yu, xu
            return "%d,%d" % (xu + ox, yu + oy)

        moves = ",".join(coord(x, y) for x, y in corners)
        self.serial.send_line("PU%s;" % moves)                # pen-up trace of the bbox

    def _v_on_test_cut(self, *_):
        if not self._jog_ready():
            return
        vs, force = self._v_cut_params()
        side = int(round(10 * C.HPGL_UNITS_PER_MM))            # 10 mm test square
        ox, oy = self._v_origin_units()                        # anchor at the Set-Origin point
        # A 10 mm square at the work origin. No IN here (it would not re-home the LH721 and
        # only muddies state); the origin is a software offset applied to these coordinates.
        # One payload so a spooled printer runs it as a single job with consistent state.
        payload = ("VS%d;FS%d;\nPU%d,%d;PD%d,%d,%d,%d,%d,%d,%d,%d;PU;\n"
                   % (vs, force,
                      ox, oy, ox + side, oy, ox + side, oy + side,
                      ox, oy + side, ox, oy))
        self.serial.send_raw(payload.encode("utf-8"), "test cut")
        self.log_view.append("INFO", "test cut (10 mm square, VS%d FS%d)" % (vs, force))

    def _v_on_send(self, *_):
        # Doubles as Stop while a cut is streaming (green Start -> red Stop), like GRBL.
        if self.serial.is_streaming():
            self._user_stopped = True                 # a Stop click is not an error
            self.serial.abort_stream(hold=False)      # hold=False: no '!' into HPGL
            self.log_view.append("WARN", "stopping cut...")
            return
        if not self.serial.is_connected():
            self.log_view.append("WARN", "connect to the machine first")
            return
        if not self._has_hpgl:
            self.log_view.append("WARN", "generate the cut first")
            return
        try:
            with open(self.temp_hpgl_path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            msg = "could not read temp HPGL: %s" % exc
            self.log_view.append("ERROR", msg)
            self._error_log_add("ERROR", msg)
            return
        if not data.strip():
            self.log_view.append("WARN", "temp HPGL is empty")
            return
        confirm = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Send the HPGL cut to '%s'?" % (self._active_port() or "the machine"))
        confirm.format_secondary_text(
            "The knife will move and cut. Make sure the material is loaded and clear. "
            "The button becomes 'Stop' while cutting.")
        proceed = confirm.run() == Gtk.ResponseType.OK
        confirm.destroy()
        if not proceed:
            self.log_view.append("INFO", "send cancelled by user")
            return
        data = self._v_apply_origin_hpgl(data)         # shift the cut to the Set-Origin point
        self._read_config_from_widgets()              # capture chunk/delay settings
        self.log_view.append(
            "INFO", "sending %d bytes to %s" % (len(data), self._active_port() or "machine"))
        # Paced, flow-controlled stream (no GRBL 'ok'): write() blocks while the cutter
        # asserts XOFF/CTS, so we never overrun its buffer.
        self.serial.stream_bytes(
            data, on_progress=self._v_on_stream_progress, on_done=self._v_on_stream_done,
            chunk_size=self.config.send_chunk, delay_s=self.config.send_delay_ms / 1000.0)
        self._begin_stream()

    def _v_on_stream_progress(self, sent: int, total: int) -> None:
        # Live progress goes to the overlay only; the Serial Log is not spammed mid-send.
        GLib.idle_add(self._set_overlay_progress, sent, total)

    def _v_on_stream_done(self, ok: bool) -> None:
        GLib.idle_add(self._end_stream, ok)

    def _v_update_action_buttons(self) -> None:
        if not hasattr(self, "v_btn_start"):
            return
        streaming = self.serial.is_streaming()
        ctx = self.v_btn_start.get_style_context()
        if streaming:
            self.v_btn_start.set_image(Gtk.Image.new_from_icon_name(
                "media-playback-stop-symbolic", Gtk.IconSize.BUTTON))
            ctx.remove_class("jg-start")
            ctx.add_class("jg-stop")
            self.v_btn_start.set_tooltip_text("Stop the running cut")
            self.v_btn_start.set_sensitive(True)
        else:
            self.v_btn_start.set_image(Gtk.Image.new_from_icon_name(
                "media-playback-start-symbolic", Gtk.IconSize.BUTTON))
            ctx.remove_class("jg-stop")
            ctx.add_class("jg-start")
            self.v_btn_start.set_tooltip_text("Send the generated HPGL cut to the machine")
            self.v_btn_start.set_sensitive(self.serial.is_connected() and self._has_hpgl)

    def _v_on_clear_print_job(self, *_):
        confirm = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL, text="Clear stuck print jobs?")
        confirm.format_secondary_text(
            "Force-restarts the Windows Print Spooler (stop, purge the queue, start) - the "
            "only reliable way to flush a job a USB cutter has wedged. This clears EVERY "
            "printer's jobs and prompts for Administrator via UAC. If you are still "
            "connected, the cutter is disconnected first so the service can stop cleanly.")
        proceed = confirm.run() == Gtk.ResponseType.OK
        confirm.destroy()
        if not proceed:
            return
        # Our own open session job would block its own deletion - close it first.
        if self.serial.is_spooling():
            self.serial.disconnect()
            self._sync_connect_button()
            self.log_view.append("INFO", "disconnected before clearing the print queue")
        # Deletes jobs and may restart the spooler, so do it off the GTK thread.
        self.v_btn_clear_job.set_sensitive(False)
        threading.Thread(target=self._clear_print_job_worker, daemon=True).start()

    def _clear_print_job_worker(self) -> None:
        try:
            ok, msg = serial_io.clear_print_jobs()
        except Exception as exc:  # noqa: BLE001 - never let the worker die silently
            ok, msg = False, "clear print jobs failed: %s" % exc
        GLib.idle_add(self._clear_print_job_done, ok, msg)

    def _clear_print_job_done(self, ok: bool, msg: str):
        self.v_btn_clear_job.set_sensitive(True)
        self.log_view.append("INFO" if ok else "WARN", msg)
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.INFO if ok else Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK,
            text="Print queue cleared" if ok else "Could not clear the print queue")
        dlg.format_secondary_text(msg)
        dlg.run()
        dlg.destroy()
        return False

    # ====================================================== data / ports
    def _extract_and_populate(self) -> None:
        try:
            self.layers = G.extract_layers(self.svg, self.uu_per_mm, self.config.flatness_mm)
        except Exception as exc:
            self.layers = []
            self._gen_log("ERROR", "could not read layers: %s" % exc)
        self.layer_table.populate(self.layers, self.config)
        text_layers = [l.label for l in self.layers if l.has_text]
        if text_layers:
            self._gen_log("INFO", "text found in: %s - will be auto-converted to paths on Generate"
                          % ", ".join(text_layers))
        self._gen_log("INFO", "loaded %d layer(s)" % len(self.layers))

    def _refresh_ports(self) -> None:
        # Both machine tabs (GRBL + Vinyl) have their own port combo; populate whichever
        # exist from the same port list so switching profile never shows a stale list.
        ports = serial_io.list_ports()
        empty = ("(no ports found)" if serial_io.pyserial_available()
                 else "pyserial not installed")
        # Each combo defaults to its own profile's saved port (GRBL -> config.port,
        # Vinyl -> config.vinyl_port); the port list itself is common to both.
        combos = [(self.c_port, self.config.port)]
        if hasattr(self, "v_c_port"):
            combos.append((self.v_c_port, self.config.vinyl_port))
        for combo, saved in combos:
            combo.remove_all()
            if not ports:
                combo.append_text(empty)
            for device, _desc in ports:
                combo.append_text(device)
            if saved:
                combo.get_child().set_text(saved)
            elif ports:
                combo.set_active(0)

    # ====================================================== config sync
    def _active_port(self) -> str:
        """Port for the current profile: Vinyl -> config.vinyl_port, GRBL -> config.port.
        Kept fully separate so each machine remembers its own USB port."""
        if self.profile == C.PROFILE_VINYL:
            return self.config.vinyl_port
        return self.config.port

    def _active_baud(self) -> int:
        """Baud for the current profile: Vinyl -> config.vinyl_baud (9600 default),
        GRBL -> config.baud (115200 default). Kept fully separate."""
        if self.profile == C.PROFILE_VINYL:
            return self.config.vinyl_baud
        return self.config.baud

    def _read_config_from_widgets(self) -> None:
        # Port and baud are per-profile and never shared: read GRBL's from c_port/c_baud
        # and Vinyl's from v_c_port/v_c_baud (below), so connecting on one profile can't
        # clobber the other's saved connection.
        self.config.port = self.c_port.get_child().get_text().strip()
        try:
            self.config.baud = int(self.c_baud.get_child().get_text())
        except ValueError:
            self.config.baud = C.DEFAULT_BAUD
        if hasattr(self, "v_adj_cut_speed"):
            self.config.cut_speed = int(self.v_adj_cut_speed.get_value())
            self.config.cut_force = int(self.v_adj_cut_force.get_value())
        if hasattr(self, "v_c_flow"):
            self.config.vinyl_port = self.v_c_port.get_child().get_text().strip()
            try:
                self.config.vinyl_baud = int(self.v_c_baud.get_child().get_text())
            except ValueError:
                self.config.vinyl_baud = C.DEFAULT_VINYL_BAUD
            self.config.flow_control = self.v_c_flow.get_active_id() or C.DEFAULT_FLOW
            self.config.send_chunk = int(self.v_adj_send_chunk.get_value())
            self.config.send_delay_ms = int(self.v_adj_send_delay.get_value())
        # Home is derived from the machine profile, not read from a widget.
        self.config.profile = self.profile
        self.config.home = C.PROFILE_HOME[self.profile]
        self.config.fill_type = self.c_fill.get_active_id() or C.FILL_HATCH
        self.config.fill_angle = float(self.s_angle.get_value())
        self.config.fill_spacing = float(self.s_spacing.get_value())
        self.config.shade_density = bool(self.chk_shade.get_active())
        self.config.mode = self.mode
        for layer, setting in self.layer_table.get_pairs():
            self.config.set_layer_setting(layer.layer_id, setting)
        self.config.save()
        self._sync_preview_machine()

    # ====================================================== logging
    def _gen_log(self, actor: str, message: str) -> None:
        """Generation/preview status. No status panel anymore: fatal errors pop a
        dialog and land in the Error Log; INFO/WARN are not surfaced (the preview is
        the feedback)."""
        if actor == "ERROR":
            self._last_error_msg = message
            self._error_log_add(actor, message)
            dlg = Gtk.MessageDialog(
                transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK, text="jasGrbl")
            dlg.format_secondary_text(message)
            dlg.run()
            dlg.destroy()

    def _log_threadsafe(self, actor: str, message: str) -> None:
        GLib.idle_add(self.log_view.append, actor, message)
        # Funnel serial-side errors/alarms into the Error Log tab too, and remember the
        # latest so a failed stream can show it in the alert dialog.
        if actor in ("ERROR", "ALARM"):
            self._last_error_msg = message
            GLib.idle_add(self._error_log_add, actor, message)

    # ====================================================== connect
    def _on_connect_clicked(self, *_):
        if self.serial.is_connected():
            self.serial.disconnect()
            self._reset_grbl_pos_readout()
        else:
            self._read_config_from_widgets()
            # GRBL paces via its 'ok' ACK, so it needs no flow control; a vinyl cutter
            # has no ACK and relies on the selected flow control to throttle the host.
            flow = self.config.flow_control if self.profile == C.PROFILE_VINYL else C.FLOW_NONE
            if self.serial.connect(self._active_port(), self._active_baud(), flow=flow):
                # Poll GRBL for its true position; an HPGL cutter would choke on '?'.
                self.serial.set_status_polling(self.profile == C.PROFILE_GRBL)
                if self.profile == C.PROFILE_VINYL:
                    # Initialise the cutter and park at origin (like the reference): IN homes
                    # the coordinate origin, SP1 selects the knife during the home cycle so
                    # later cuts don't detour to the pen station. Reset the tracked head and
                    # the software work-origin (a fresh connection starts at 0,0).
                    self.serial.send_line("IN;SP1;PU0,0;")
                    self._v_head_mm = [0.0, 0.0]
                    self._v_origin_mm = [0.0, 0.0]
                    self._v_update_pos_label()
        self._sync_connect_button()

    def _sync_connect_button(self) -> None:
        connected = self.serial.is_connected()

        def sync_btn(btn):
            ctx = btn.get_style_context()
            if connected:
                btn.set_label("Disconnect")
                ctx.remove_class("jg-connect")
                ctx.add_class("jg-disconnect")
            else:
                btn.set_label("Connect")
                ctx.remove_class("jg-disconnect")
                ctx.add_class("jg-connect")

        sync_btn(self.btn_connect)
        self._update_conn_status()
        self.c_port.set_sensitive(not connected)
        self.c_baud.set_sensitive(not connected)
        self.btn_cmd_send.set_sensitive(connected)
        self.e_command.set_sensitive(connected)
        if hasattr(self, "btn_grbl_setting"):
            self.btn_grbl_setting.set_sensitive(connected)
        if hasattr(self, "jog_frame"):
            self.jog_frame.set_sensitive(connected)
        # Vinyl tab mirrors the same connection state (both tabs share self.serial).
        if hasattr(self, "v_btn_connect"):
            sync_btn(self.v_btn_connect)
            self.v_c_port.set_sensitive(not connected)
            self.v_c_baud.set_sensitive(not connected)
            self.v_btn_reset.set_sensitive(connected)
            # Gate only the connection-dependent jog controls; Clear Print Job (also in
            # the jog frame) stays enabled so a wedged job is clearable while offline.
            for widget in getattr(self, "_v_conn_widgets", []):
                widget.set_sensitive(connected)
        self._update_action_buttons()

    def _on_send(self, *_):
        text = self.e_command.get_text()
        if text.strip():
            self.serial.send_line(text)
            self.e_command.set_text("")

    def _on_grbl_setting(self, *_):
        if self.serial.is_connected():
            self.log_view.append("INFO", "Query GRBL settings ($$)")
            self.serial.send_line("$$")

    # ====================================================== jog
    def _jog_ready(self) -> bool:
        if not self.serial.is_connected():
            self.log_view.append("WARN", "connect to the machine first")
            return False
        if self.serial.is_streaming():
            self.log_view.append("WARN", "a job is running; stop it before jogging")
            return False
        return True

    def _grbl_pos(self) -> str:
        """The true work position parsed from GRBL status reports, for the Serial Log."""
        if not self._grbl_pos_known:
            return "X? Y? (no status yet)"
        return "X%.2f Y%.2f" % (self._grbl_x, self._grbl_y)

    def _on_grbl_status(self, state: str, x: float, y: float) -> None:
        # Called on the serial reader thread; hop to the GTK thread to touch widgets.
        GLib.idle_add(self._apply_grbl_status, state, x, y)

    def _apply_grbl_status(self, state: str, x: float, y: float) -> bool:
        self._grbl_state = state
        self._grbl_x, self._grbl_y = x, y
        self._grbl_pos_known = True
        if hasattr(self, "lbl_grbl_pos"):
            self.lbl_grbl_pos.set_text("Pos: X%.2f  Y%.2f   [%s]" % (x, y, state or "-"))
        if hasattr(self, "lbl_canvas_pos"):
            self.lbl_canvas_pos.set_text("X %.1f   Y %.1f" % (x, y))
        return False        # one-shot GLib.idle_add callback

    def _reset_grbl_pos_readout(self) -> None:
        self._grbl_pos_known = False
        self._grbl_state = ""
        if hasattr(self, "lbl_grbl_pos"):
            self.lbl_grbl_pos.set_text("Pos: —")
        if hasattr(self, "lbl_canvas_pos"):
            self.lbl_canvas_pos.set_text("X 0.0   Y 0.0")

    def _log_action_then_pos(self, desc: str, settle_ms: int = 500) -> None:
        """Log a GRBL action now, then log the resulting true position once the move has
        settled (status reports keep _grbl_x/_y live)."""
        self.log_view.append("INFO", desc)
        GLib.timeout_add(settle_ms, self._log_settled_pos, desc)

    def _log_settled_pos(self, desc: str) -> bool:
        self.log_view.append("INFO", "%s done  →  %s" % (desc, self._grbl_pos()))
        return False

    @staticmethod
    def _jog_dir_label(dx: int, dy: int) -> str:
        return ("+X" if dx > 0 else "-X") if dx else ("+Y" if dy > 0 else "-Y")

    def _jog_command(self, dx: int, dy: int, dist: float) -> None:
        feed = int(self.adj_jog_feed.get_value())
        parts = []
        if dx:
            parts.append("X%g" % (dx * dist))
        if dy:
            parts.append("Y%g" % (dy * dist))
        self.serial.send_line("$J=G91 G21 %s F%d" % (" ".join(parts), feed))

    def _on_jog_step(self, dx: int, dy: int) -> None:
        # In continuous mode the press/release handlers own the jog; ignore the click.
        if self.chk_jog_continuous.get_active():
            return
        if not self._jog_ready():
            return
        dist = float(self.adj_jog_step.get_value())
        self._jog_command(dx, dy, dist)
        # Position comes from GRBL status reports; log the action, then the settled X/Y.
        self._log_action_then_pos("Jog %s %gmm" % (self._jog_dir_label(dx, dy), dist))

    def _on_jog_continuous_start(self, dx: int, dy: int) -> None:
        if not self.chk_jog_continuous.get_active():
            return
        if not self._jog_ready():
            return
        # Jog toward the far edge of the work area; release cancels it (0x85).
        reach = max(self.doc_w_mm, self.doc_h_mm) or 500.0
        self._jog_command(dx, dy, reach)
        self.log_view.append(
            "INFO", "Jog %s (continuous, hold)  →  from %s"
            % (self._jog_dir_label(dx, dy), self._grbl_pos()))

    def _on_jog_continuous_stop(self) -> None:
        if self.chk_jog_continuous.get_active() and self.serial.is_connected():
            self.serial.jog_cancel()
            # Status reports keep the position live, so just log where it ended up.
            self._log_action_then_pos("Jog stopped")

    def _on_home(self, *_):
        if self._jog_ready():
            self.serial.send_line("$H")
            # Homing can take seconds; the live readout tracks it. Log the final position
            # once settled (a generous wait so the cycle can finish).
            self._log_action_then_pos("Homing ($H)", settle_ms=4000)

    def _on_set_home(self, *_):
        # Zero the active work coordinate system at the current position.
        if self._jog_ready():
            self.serial.send_line("G10 L20 P1 X0 Y0")
            self._log_action_then_pos("Set Home: work origin")

    def _on_frame(self, *_):
        if not self._jog_ready():
            return
        if self.program is None or not self.program.segments:
            self.log_view.append("WARN", "generate G-code first to frame the job")
            return
        pts = []
        for _kind, a, b in self.program.segments:
            pts.append(a)
            pts.append(b)
        xmin, ymin, xmax, ymax = G.bbox(pts)
        feed = int(self.adj_jog_feed.get_value())
        corners = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax), (xmin, ymin)]
        self.serial.send_line("M5")                       # laser off for framing
        self.serial.send_line("G90")
        self.serial.send_line("G0 X%g Y%g" % corners[0])
        for cx, cy in corners[1:]:
            self.serial.send_line("G1 X%g Y%g F%d" % (cx, cy, feed))
        self.log_view.append(
            "INFO", "Frame job bbox X%.1f..%.1f Y%.1f..%.1f" % (xmin, xmax, ymin, ymax))

    def _on_test_laser(self, *_):
        if not self._jog_ready():
            return
        power = max(1, int(self.config.s_max * 0.05))      # brief 5% pulse
        # GRBL laser mode ($32=1) refuses to turn the laser on while the machine
        # is idle (it only fires during motion) - so a stationary test pulse never
        # lights up, even with M3 constant power. Like LaserGRBL/LightBurn's "fire"
        # button, briefly leave laser mode ($32=0), fire with M3, then M5 and
        # restore laser mode ($32=1). The 0.3 s pulse is timed with a non-blocking
        # GLib.timeout_add so the UI stays responsive.
        self.serial.send_line("$32=0")                     # allow idle firing
        self.serial.send_line("M3 S%d" % power)
        GLib.timeout_add(300, self._test_laser_off)
        self.log_view.append("INFO", "laser test pulse (S%d, 0.3s)" % power)

    def _test_laser_off(self):
        self.serial.send_line("M5")
        self.serial.send_line("$32=1")                     # restore laser mode
        return False        # one-shot GLib.timeout_add callback

    # ====================================================== generate
    def _set_generate_state(self, done: bool) -> None:
        """Generate button look: accent 'Generate' vs green '✓ Generated' once output
        exists (mirrors the design's ready-to-send state)."""
        ctx = self.btn_generate.get_style_context()
        if done:
            ctx.add_class("jg-generate-done")
            self.btn_generate.set_label("✓ Generated — ready to send")
        else:
            ctx.remove_class("jg-generate-done")
            self.btn_generate.set_label("Generate G-code")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_generate.set_sensitive(not busy)
        self.btn_clear.set_sensitive(not busy)
        if busy:
            # Accent 'Generating…' while the worker runs (drop any prior done state).
            self.btn_generate.get_style_context().remove_class("jg-generate-done")
            self.btn_generate.set_label("Generating...")
            self.btn_export.set_sensitive(False)
            self._show_overlay("Generating G-code…")
        else:
            # _generate_done sets the ✓ done state on success; reset to plain otherwise.
            self.btn_generate.set_label("Generate G-code")
            self._hide_overlay()

    def _on_generate(self, *_):
        if self._busy:
            return
        self._read_config_from_widgets()
        settings_map = {ld.layer_id: s for ld, s in self.layer_table.get_pairs()}
        if not any(s.enabled for s in settings_map.values()):
            self._gen_log("WARN", "no layers enabled; nothing to generate")
            return
        options = GenOptions(
            version=__version__,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            s_max=self.config.s_max,
            laser_mode=self.config.laser_mode,
            mode=self.mode,
            # Vinyl cutters transpose HPGL X/Y vs the carriage, so the HPGL cut always swaps
            # (x,y)->(y,x). GRBL output is never swapped. (See _v_swap_xy / MachineSpace.)
            swap_xy=(self.profile == C.PROFILE_VINYL),
        )
        # Run the slow work (text->path subprocess, flattening, fills) off the GTK thread
        # so the dialog stays responsive and shows a spinner instead of freezing blank.
        self._set_busy(True)
        threading.Thread(
            target=self._generate_worker, args=(options, settings_map),
            daemon=True).start()

    def _generate_worker(self, options, settings_map) -> None:
        try:
            tree = self.svg
            if G.svg_has_text(self.svg):
                converted = G.text_to_path_root(self.svg, None)
                if converted is not None:
                    tree = converted
            fresh = G.extract_layers(tree, self.uu_per_mm, self.config.flatness_mm)
            # Work area = Inkscape document; the design keeps its document position.
            machine = G.MachineSpace(self.doc_w_mm, self.doc_h_mm, self.config.home)
            pairs = [(ld, settings_map.get(ld.layer_id, LayerSetting())) for ld in fresh]
            fill_params = FillParams(spacing=self.config.fill_spacing,
                                     angle=self.config.fill_angle, log=None,
                                     shade_density=self.config.shade_density)
            # Plan once, emit both back-ends so preview + GRBL + HPGL agree exactly.
            blocks = plan_toolpaths(
                pairs, machine, self.config.fill_type, fill_params, options.mode)
            program = emit_grbl(blocks, machine, options, log=None)
            gcode_text = program.text()
            hpgl_text = emit_hpgl(blocks, machine, options, log=None)
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._generate_done, None, None, None, str(exc))
            return
        GLib.idle_add(self._generate_done, program, gcode_text, hpgl_text, None)

    def _generate_done(self, program, gcode_text, hpgl_text, error):
        self._set_busy(False)
        if error is not None:
            self._gen_log("ERROR", "generation failed: %s" % error)
            return False
        self.program = program
        try:
            with open(self.temp_gcode_path, "w", encoding="utf-8") as fh:
                fh.write(gcode_text)
            self._has_gcode = True
        except OSError as exc:
            self._gen_log("ERROR", "could not write temp G-code: %s" % exc)
        try:
            with open(self.temp_hpgl_path, "w", encoding="utf-8") as fh:
                fh.write(hpgl_text)
            self._has_hpgl = True
        except OSError as exc:
            self._gen_log("ERROR", "could not write temp HPGL: %s" % exc)
        self.preview.set_program(program)
        self._set_generate_state(True)
        self._update_action_buttons()
        return False

    # ------------------------------------------------ output mode
    def _on_mode_changed(self, btn):
        # "toggled" fires for both the deactivating and activating radio; act once.
        if not btn.get_active():
            return
        new_mode = C.MODE_PLOTTER if btn is self.rb_plotter else C.MODE_ENGRAVING
        if new_mode == self.mode:
            return
        self.mode = new_mode
        # Engraving vs Plotter change the emitted code, so any existing output is now
        # stale: clear it (skip if already empty - nothing to clear).
        if self._has_gcode or self._has_hpgl or self.program is not None:
            self._clear_output()
        self.layer_table.set_mode(new_mode)
        self._update_status_labels()

    # ------------------------------------------------ machine profile
    def _on_profile_changed(self, btn):
        # "toggled" fires for both the deactivating and activating radio; act once.
        if not btn.get_active():
            return
        new_profile = C.PROFILE_VINYL if btn is self.rb_vinyl else C.PROFILE_GRBL
        if new_profile == self.profile:
            return
        self.profile = new_profile
        # The profile fixes the Home corner; re-map the axes immediately so the preview
        # shows the new coordinate system.
        self.config.profile = new_profile
        self.config.home = C.PROFILE_HOME[new_profile]
        self._sync_preview_machine()
        # Switching profile changes the Home (and thus every coordinate) and the export
        # format, so any generated output is stale: clear and regenerate from scratch.
        self._clear_output()
        self._update_export_tooltip()
        self._update_status_labels()
        self._update_machine_tabs()
        # Status polling ('?') is a GRBL-only trick; re-gate it if we are connected.
        if self.serial.is_connected():
            self.serial.set_status_polling(new_profile == C.PROFILE_GRBL)
            if new_profile != C.PROFILE_GRBL:
                self._reset_grbl_pos_readout()

    # ------------------------------------------------ export format (per profile)
    def _export_target(self):
        """(has_output, temp_path, extension, dialog title) for the active profile."""
        if self.profile == C.PROFILE_VINYL:
            return (self._has_hpgl, self.temp_hpgl_path, C.HPGL_EXT, "Save HPGL")
        return (self._has_gcode, self.temp_gcode_path, ".gcode", "Save GCode")

    def _has_current_output(self) -> bool:
        return self._export_target()[0]

    def _update_export_tooltip(self) -> None:
        if self.profile == C.PROFILE_VINYL:
            self.btn_export.set_tooltip_text("Save the generated HPGL plot (Vinyl Cutter)")
        else:
            self.btn_export.set_tooltip_text("Save the generated GRBL G-code")

    # ------------------------------------------------ temp code files
    def _delete_temp_files(self) -> None:
        for path in (self.temp_gcode_path, self.temp_hpgl_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    def _read_temp_gcode_lines(self) -> List[str]:
        try:
            with open(self.temp_gcode_path, "r", encoding="utf-8") as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    # ------------------------------------------------ export
    def _on_export(self, *_):
        has_output, temp_path, ext, title = self._export_target()
        if not has_output:
            self._gen_log("WARN", "generate first")
            return
        self._save_temp_as(title, temp_path, ext)

    def _save_temp_as(self, title: str, temp_path: str, ext: str) -> None:
        dialog = Gtk.FileChooserDialog(
            title=title, transient_for=self, action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dialog.set_current_name("printable" + ext)
        out = self._default_output_dir()
        if out:
            dialog.set_current_folder(out)
        if dialog.run() == Gtk.ResponseType.OK:
            path = dialog.get_filename()
            try:
                with open(temp_path, "r", encoding="utf-8") as src:
                    data = src.read()
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(data)
                self._gen_log("INFO", "exported to %s" % path)
            except OSError as exc:
                self._gen_log("ERROR", "export failed: %s" % exc)
        dialog.destroy()

    def _default_output_dir(self) -> Optional[str]:
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        out = os.path.join(here, "releases", "output")
        try:
            os.makedirs(out, exist_ok=True)
            return out
        except OSError:
            return None

    # ------------------------------------------------ send to machine
    def _on_send_to_machine(self, *_):
        if self.serial.is_streaming():
            self._user_stopped = True          # a Stop click is not an error
            self.serial.abort_stream()
            self.log_view.append("WARN", "stopping stream...")
            return
        if not self.serial.is_connected():
            self.log_view.append("WARN", "connect to the machine first")
            return
        if not self._has_gcode:
            self.log_view.append("WARN", "generate G-code first")
            return
        lines = self._read_temp_gcode_lines()
        n = len([l for l in lines if l.strip() and not l.strip().startswith(";")])
        if n == 0:
            self.log_view.append("WARN", "temp G-code is empty")
            return
        confirm = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Send %d G-code lines to '%s'?" % (n, self._active_port() or "the machine"))
        confirm.format_secondary_text(
            "The laser will move and fire. Make sure the work area is clear. "
            "The button becomes 'Stop' while running.")
        proceed = confirm.run() == Gtk.ResponseType.OK
        confirm.destroy()
        if not proceed:
            self.log_view.append("INFO", "send cancelled by user")
            return
        # Remember the prepared program so a partial job can be resumed from the last
        # acknowledged line (indices line up with serial's last_acked_index).
        self._stream_program = serial_io.SerialManager._prepare_program(lines)
        self._resume_index = 0
        self.log_view.append(
            "INFO", "streaming %d lines to %s" % (n, self._active_port() or "machine"))
        self.serial.stream(lines, on_progress=self._on_stream_progress,
                           on_done=self._on_stream_done)
        self._begin_stream()

    def _on_stream_progress(self, sent: int, total: int) -> None:
        # Live progress goes to the overlay only; the Serial Log is not spammed mid-stream
        # (it just marks the start, the end, and any error/alarm).
        GLib.idle_add(self._set_overlay_progress, sent, total)

    def _on_stream_done(self, ok: bool) -> None:
        GLib.idle_add(self._end_stream, ok)

    # ====================================================== action buttons
    def _update_action_buttons(self) -> None:
        self._v_update_action_buttons()
        if not hasattr(self, "btn_start"):
            return
        streaming = self.serial.is_streaming()
        self.btn_generate.set_sensitive(not streaming)
        self.btn_clear.set_sensitive(not streaming)
        # Save stays disabled until the profile's output has been generated.
        self.btn_export.set_sensitive(self._has_current_output() and not streaming)
        # The jog Start button doubles as the job control: green Start -> red Stop.
        ctx = self.btn_start.get_style_context()
        if streaming:
            self._set_start_icon("media-playback-stop-symbolic")
            ctx.remove_class("jg-start")
            ctx.add_class("jg-stop")
            self.btn_start.set_tooltip_text("Stop the running job")
            self.btn_start.set_sensitive(True)
        else:
            self._set_start_icon("media-playback-start-symbolic")
            ctx.remove_class("jg-stop")
            ctx.add_class("jg-start")
            self.btn_start.set_tooltip_text("Start the generated job")
            self.btn_start.set_sensitive(self.serial.is_connected() and self._has_gcode)
        # Pause/Resume: live only while a GRBL job streams (feed-hold is not valid for the
        # HPGL/vinyl byte stream). Toggles icon + tooltip to reflect the paused state.
        if hasattr(self, "btn_pause"):
            can_pause = streaming and self.profile == C.PROFILE_GRBL
            self.btn_pause.set_sensitive(can_pause)
            if can_pause and self.serial.is_paused():
                self._set_pause_icon("media-playback-start-symbolic")
                self.btn_pause.set_tooltip_text("Resume the paused job")
            else:
                self._set_pause_icon("media-playback-pause-symbolic")
                self.btn_pause.set_tooltip_text("Pause the running job")

    def _on_pause_resume(self, *_):
        """Toggle pause/resume on the running GRBL stream. Pause sends a feed-hold and
        turns the laser/spindle off; resume sends cycle-start and refills the buffer."""
        if not self.serial.is_streaming():
            return
        if self.serial.is_paused():
            self.serial.resume()
        else:
            self.serial.feed_hold()
        self._update_action_buttons()          # flip the icon immediately

    # ====================================================== streaming lock
    def _collect_stream_lock_widgets(self) -> None:
        """Everything to disable while a job streams - all setting/generate/connect/jog
        controls EXCEPT the Start/Stop button (btn_start / v_btn_start) and Clear Print
        Job. Built once, after all tabs exist."""
        widgets = [self._gcode_tab, self.connect_frame, self.e_command, self.btn_cmd_send]
        widgets += getattr(self, "_grbl_lock_widgets", [])
        if hasattr(self, "v_connect_frame"):
            widgets.append(self.v_connect_frame)
        widgets += getattr(self, "_v_conn_widgets", [])
        self._stream_lock_widgets = widgets

    def _set_stream_lock(self, locked: bool) -> None:
        """Lock/unlock the UI for streaming and show/hide the 'Sending…' overlay."""
        for widget in getattr(self, "_stream_lock_widgets", []):
            widget.set_sensitive(not locked)
        if locked:
            self._show_overlay("Sending…")
        else:
            self._hide_overlay()
            # Restore the correct per-widget sensitivity (connected state, jog gating,
            # action buttons) now that the blanket lock is lifted.
            self._sync_connect_button()

    def _begin_stream(self) -> None:
        self._user_stopped = False
        self._last_error_msg = ""
        self._set_stream_lock(True)
        self._update_action_buttons()          # flip Start -> red Stop

    def _end_stream(self, ok: bool) -> bool:
        self._set_stream_lock(False)           # also re-syncs the action buttons
        if not ok and not self._user_stopped:
            self._alert_error(
                "Stream failed", self._last_error_msg or "the stream stopped unexpectedly")
        if not ok:
            self._maybe_offer_resume()
        return False                            # usable as a GLib.idle_add callback

    # ====================================================== resume
    def _effective_acked(self) -> int:
        """Index (into self._stream_program) of the last line GRBL confirmed, across any
        chained resumes: the base offset of the current run plus this run's acked count."""
        return self._resume_index + self.serial.last_acked_index

    def _maybe_offer_resume(self) -> None:
        """After a GRBL job stops/errors partway, offer to resume from the last acked line.

        Resume replays no motion: it reconstructs the G-code modal state (units, distance
        mode, feed, laser/spindle power), rapids to the stop point with the beam OFF, then
        continues. It cannot verify the machine's true position, so the user must confirm
        the machine has not been moved / has been re-homed since the stop."""
        if self.profile != C.PROFILE_GRBL or not self.serial.is_connected():
            return
        prog = self._stream_program
        acked = self._effective_acked()
        if not prog or acked <= 0 or acked >= len(prog):
            return
        preamble, reason = serial_io.build_resume_preamble(prog, acked)
        if preamble is None:
            self._alert_error(
                "Cannot resume safely",
                "Stopped at line %d of %d, but a safe resume point could not be built:\n\n%s\n\n"
                "Re-run the whole job instead." % (acked, len(prog), reason))
            return
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="Resume job from line %d of %d?" % (acked, len(prog)))
        dlg.format_secondary_text(
            "The tool will move to the stop point (with the laser/spindle OFF) and continue.\n"
            "Make sure the machine has NOT been moved or re-home it first, otherwise the "
            "resume position will be wrong.")
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        resume_btn = dlg.add_button("Resume", Gtk.ResponseType.OK)
        resume_btn.get_style_context().add_class("suggested-action")
        proceed = dlg.run() == Gtk.ResponseType.OK
        dlg.destroy()
        if proceed:
            self._resume_stream(acked, preamble)

    def _resume_stream(self, from_index: int, preamble: List[str]) -> None:
        remaining = self._stream_program[from_index:]
        if not remaining:
            return
        self.log_view.append("INFO", "resuming from line %d (%d lines remain)"
                             % (from_index, len(remaining)))
        self._resume_index = from_index        # base offset for chained resumes
        self.serial.stream(remaining, on_progress=self._on_stream_progress,
                           on_done=self._on_stream_done, preamble=preamble)
        self._begin_stream()

    def _alert_error(self, title: str, message: str) -> None:
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK, text=title)
        dlg.format_secondary_text(message)
        dlg.run()
        dlg.destroy()

    # ====================================================== clear / destroy
    def _clear_output(self) -> None:
        """Drop the generated output: temp GRBL + HPGL files + in-dialog preview
        toolpath. Keeps the grid / axes / Home / layer settings."""
        self.program = None
        self._has_gcode = False
        self._has_hpgl = False
        self._delete_temp_files()
        self.preview.clear_paths()
        self._set_generate_state(False)
        self._update_action_buttons()

    def _on_clear(self, *_):
        if self.serial.is_streaming():
            return
        self._clear_output()
        self.layer_table.reset_defaults()

    def _on_destroy(self, *_):
        try:
            self._read_config_from_widgets()
        except Exception:
            pass
        self.serial.abort_stream()
        self.serial.disconnect()
        self._delete_temp_files()
        Gtk.main_quit()


def launch(svg) -> None:
    """Build, show and run the dialog modally (blocks until the window closes)."""
    win = JasGrblWindow(svg)
    win.show_all()
    win.notebook.set_current_page(0)     # always start on the Generate tab
    win._sync_connect_button()
    Gtk.main()
