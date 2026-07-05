"""Theme + CSS styling for the jasGrbl dialog (GTK 3).

The dialog ships its own dark "console" look (imported from the Claude Design mock
``jas GRBL.dc.html``): a near-black app surface, panel/card frames, an accent colour
used for the active tab underline + primary action, and JetBrains-Mono numerics. We
launch in a separate process, so we force the dark theme preference and layer the whole
palette on top with an app-priority CSS provider, scoped under the ``.jg-root`` class on
our window so GTK's own dialogs (file chooser, message dialogs) keep the native theme.

Change ACCENT below to re-tint the whole UI (the design offers orange #e8710a,
blue #2563d6, green #3fb950, magenta #c026d3, red #e11d48).
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

# --------------------------------------------------------------------------- palette
# Primary accent (design default: magenta). One knob re-tints tabs, the primary
# Generate button, jog Home, focus rings and the origin dot in the preview.
ACCENT = "#c026d3"


def _darken(hex_color: str, factor: float = 0.78) -> str:
    """Multiply an ``#rrggbb`` colour toward black (matches the design's _darken)."""
    n = int(hex_color.lstrip("#"), 16)
    r = int(((n >> 16) & 255) * factor)
    g = int(((n >> 8) & 255) * factor)
    b = int((n & 255) * factor)
    return "#%02x%02x%02x" % (r, g, b)


ACCENT_DARK = _darken(ACCENT)

# Surfaces, borders and text (design tokens).
BG = "#0d1117"          # app surface
BG_CANVAS = "#0a0d12"   # left preview pane / input fields
PANEL = "#161d27"       # title/header bars, table header, default buttons
CARD = "#0f151d"        # framed cards (Connect / Jog / Cut Setting)
BORDER = "#222a35"       # card + separator borders
BORDER2 = "#2a323d"      # input borders
BORDER_FAINT = "#1a212b"  # inner hairlines
TEXT = "#e6edf3"         # primary text
TEXT2 = "#c9d4e0"        # slightly dim primary
TEXTDIM = "#8b949e"      # labels / secondary
TEXTFAINT = "#5c6672"    # captions / section headers
TEXTFAINTEST = "#3a4351"  # disabled / empty state

# Semantic colours.
GREEN = "#3fb950"
GREEN_DARK = "#2ea043"
RED = "#c9412e"
RED_DARK = "#a5301f"
BLUE = "#2563d6"          # Engraving mode pill
BLUE_LT = "#58a6ff"       # checkbox / info accents
PURPLE = "#7c4ddb"        # Plotter mode pill
VINYL = "#2b8fa8"         # Vinyl Cutter profile pill

FONT_UI = '"Space Grotesk", "Cantarell", "Segoe UI", sans-serif'
FONT_MONO = '"JetBrains Mono", "DejaVu Sans Mono", monospace'


def _build_css() -> bytes:
    return ("""
@define-color jg_accent %(ACCENT)s;
@define-color jg_accent_dark %(ACCENT_DARK)s;
@define-color jg_bg %(BG)s;
@define-color jg_panel %(PANEL)s;
@define-color jg_card %(CARD)s;
@define-color jg_border %(BORDER)s;
@define-color jg_text %(TEXT)s;

/* Re-point the base theme's own colour variables at the design palette so any surface
   we don't explicitly style (notebook body, viewports, popovers, menus) is painted with
   the design's dark tones instead of Adwaita-dark's grey - killing the "mixed" look. */
@define-color theme_bg_color %(BG)s;
@define-color theme_base_color %(BG_CANVAS)s;
@define-color theme_fg_color %(TEXT)s;
@define-color theme_text_color %(TEXT)s;
@define-color theme_selected_bg_color %(ACCENT)s;
@define-color theme_selected_fg_color #ffffff;
@define-color theme_unfocused_bg_color %(BG)s;
@define-color theme_unfocused_base_color %(BG_CANVAS)s;
@define-color theme_unfocused_fg_color %(TEXT)s;
@define-color theme_unfocused_text_color %(TEXT)s;
@define-color insensitive_bg_color %(BG)s;
@define-color insensitive_fg_color %(TEXTFAINTEST)s;
@define-color borders %(BORDER)s;
@define-color unfocused_borders %(BORDER)s;
@define-color content_view_bg %(BG)s;
@define-color window_bg_color %(BG)s;
@define-color view_bg_color %(BG_CANVAS)s;
@define-color popover_bg_color %(PANEL)s;

/* ------------------------------------------------------------ base surface */
.jg-root {
    background-color: %(BG)s;
    color: %(TEXT)s;
    font-family: %(FONT_UI)s;
    font-size: 10pt;
}
.jg-root label { color: %(TEXT2)s; }
.jg-root separator { background-color: %(BORDER)s; }

/* Surfaces that would otherwise paint the base theme's grey (leaking through as a
   "mixed" look): notebook body, stacks, viewports, scrolled areas, overlays, paned. */
.jg-root notebook,
.jg-root notebook stack,
.jg-root stack,
.jg-root scrolledwindow,
.jg-root viewport,
.jg-root overlay,
.jg-root paned {
    background-color: %(BG)s;
}

/* ------------------------------------------------------------ entries / combos */
.jg-root entry {
    background-color: %(BG_CANVAS)s;
    background-image: none;
    color: %(TEXT)s;
    border: 1px solid %(BORDER2)s;
    border-radius: 8px;
    padding: 5px 8px;
    caret-color: %(ACCENT)s;
}
.jg-root entry:focus { border-color: %(ACCENT)s; }
.jg-root entry image { color: %(TEXTDIM)s; }
.jg-root entry selection { background-color: %(ACCENT)s; color: #ffffff; }

.jg-root combobox box.linked > button.combo {
    background-image: none;
    background-color: %(BG_CANVAS)s;
    color: %(TEXT)s;
    border: 1px solid %(BORDER2)s;
    border-radius: 8px;
}
.jg-root combobox arrow { color: %(TEXTDIM)s; min-height: 14px; min-width: 14px; }
.jg-root combobox entry { border-radius: 8px; }

/* ------------------------------------------------------------ spinbuttons */
.jg-root spinbutton {
    background-color: %(BG_CANVAS)s;
    background-image: none;
    color: %(TEXT)s;
    border: 1px solid %(BORDER2)s;
    border-radius: 8px;
    font-family: %(FONT_MONO)s;
}
.jg-root spinbutton entry {
    border: none;
    background: none;
    border-radius: 8px;
}
.jg-root spinbutton button {
    background-image: none;
    background-color: transparent;
    color: %(TEXTDIM)s;
    border: none;
    border-radius: 0;
}
.jg-root spinbutton button:hover { color: %(TEXT)s; }

/* ------------------------------------------------------------ buttons */
.jg-root button {
    background-image: none;
    background-color: %(PANEL)s;
    color: %(TEXT2)s;
    border: 1px solid %(BORDER2)s;
    border-radius: 8px;
    padding: 6px 12px;
    text-shadow: none;
}
.jg-root button:hover { background-color: #1c2531; }
.jg-root button:active { background-color: #10161f; }
.jg-root button:disabled {
    background-color: %(BG_CANVAS)s;
    color: %(TEXTFAINTEST)s;
    border-color: %(BORDER)s;
}
.jg-root button image { color: inherit; }

/* Flat, borderless buttons (e.g. the layer-row eye toggle) - no chip look, no extra
   padding, so they don't inflate the row height. */
.jg-root button.jg-flat {
    background-color: transparent;
    background-image: none;
    border: none;
    box-shadow: none;
    padding: 2px;
    min-height: 0;
    min-width: 0;
}
.jg-root button.jg-flat:hover { background-color: transparent; }
.jg-root button.jg-flat:checked { background-color: transparent; }

/* Primary Generate: accent gradient; turns green once output exists. */
.jg-root button.jg-generate {
    background-image: linear-gradient(to bottom, %(ACCENT)s, %(ACCENT_DARK)s);
    color: #ffffff;
    border: none;
    font-weight: bold;
    border-radius: 10px;
    padding: 8px 14px;
}
.jg-root button.jg-generate:hover {
    background-image: linear-gradient(to bottom, %(ACCENT)s, %(ACCENT)s);
}
.jg-root button.jg-generate-done {
    background-image: linear-gradient(to bottom, %(GREEN)s, %(GREEN_DARK)s);
    color: #ffffff;
    border: none;
    font-weight: bold;
    border-radius: 10px;
}

/* Connect (green) <-> Disconnect (red). */
.jg-root button.jg-connect {
    background-image: linear-gradient(to bottom, %(GREEN)s, %(GREEN_DARK)s);
    color: #ffffff; border: none; font-weight: bold; border-radius: 9px;
}
.jg-root button.jg-disconnect {
    background-image: linear-gradient(to bottom, %(RED)s, %(RED_DARK)s);
    color: #ffffff; border: none; font-weight: bold; border-radius: 9px;
}

/* Jog Start (green) -> Stop (red) while streaming. */
.jg-root button.jg-start {
    background-image: linear-gradient(to bottom, %(GREEN)s, %(GREEN_DARK)s);
    color: #ffffff; border: none; border-radius: 9px;
}
.jg-root button.jg-stop {
    background-image: linear-gradient(to bottom, %(RED)s, %(RED_DARK)s);
    color: #ffffff; border: none; border-radius: 9px;
}

/* Directional jog pad. */
.jg-root button.jg-jog {
    background-color: %(PANEL)s;
    color: %(TEXT2)s;
    border: 1px solid %(BORDER2)s;
    border-radius: 9px;
    font-family: %(FONT_MONO)s;
    font-weight: bold;
    padding: 0;
    min-width: 40px;
    min-height: 40px;
}
.jg-root button.jg-jog:hover { background-color: #1c2531; border-color: %(ACCENT)s; }
.jg-root button.jg-jog-home {
    background-color: #1f1710;
    color: %(ACCENT)s;
    border: 1px solid %(ACCENT)s;
    border-radius: 9px;
    min-width: 40px;
    min-height: 40px;
}

/* Test Laser / Test Cut: warm danger tint. */
.jg-root button.jg-test {
    background-color: #2a1410;
    color: #ff8a5c;
    border: 1px solid rgba(176, 64, 32, 0.4);
    border-radius: 9px;
}

/* ------------------------------------------------------------ -/+ stepper */
.jg-root box.jg-stepper {
    background-color: %(BG_CANVAS)s;
    border: 1px solid %(BORDER2)s;
    border-radius: 8px;
}
.jg-root box.jg-stepper entry {
    border: none;
    background: none;
    font-family: %(FONT_MONO)s;
    color: %(TEXT)s;
}
.jg-root box.jg-stepper button {
    background-color: transparent;
    background-image: none;
    color: %(TEXTDIM)s;
    border: none;
    border-radius: 0;
    padding: 0 10px;
    font-size: 14pt;
}
.jg-root box.jg-stepper button:hover { color: %(TEXT)s; background-color: transparent; }

/* ------------------------------------------------------------ toggle pills */
.jg-root box.jg-toggle-group {
    background-color: %(BG_CANVAS)s;
    border: 1px solid %(BORDER)s;
    border-radius: 9px;
    padding: 3px;
}
.jg-root box.linked > button.jg-mode,
.jg-root box.linked > button.jg-profile {
    background-image: none;
    background-color: transparent;
    color: %(TEXTDIM)s;
    border: none;
    border-radius: 7px;
    font-weight: bold;
    padding: 5px 12px;
}
.jg-root box.linked > button.jg-mode:checked {
    background-color: %(BLUE)s; color: #ffffff; border-color: %(BLUE)s;
}
.jg-root box.linked > button.jg-profile:checked {
    background-color: %(GREEN_DARK)s; color: #ffffff; border-color: %(GREEN_DARK)s;
}

/* ------------------------------------------------------------ notebook tabs */
.jg-root notebook > header {
    background-color: %(BG)s;
    border-color: %(BORDER)s;
}
.jg-root notebook > header tab {
    background-image: none;
    background-color: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    box-shadow: none;
    padding: 8px 16px;
    color: %(TEXTDIM)s;
    font-weight: bold;
}
.jg-root notebook > header tab:checked {
    border-bottom: 2px solid %(ACCENT)s;
    color: %(TEXT)s;
    background-color: transparent;
}
.jg-root notebook > header tab:hover { color: %(TEXT2)s; }

/* ------------------------------------------------------------ frames (cards) */
.jg-root frame {
    background-color: %(CARD)s;
    border-radius: 12px;
}
.jg-root frame > border {
    border: 1px solid %(BORDER)s;
    border-radius: 12px;
}
.jg-root frame > label {
    color: %(TEXTFAINT)s;
    font-weight: bold;
    font-size: 9pt;
}

/* ------------------------------------------------------------ scales (jog feed) */
.jg-root scale trough {
    background-color: %(BG_CANVAS)s;
    border: 1px solid %(BORDER2)s;
    border-radius: 6px;
    min-width: 6px;
}
.jg-root scale highlight {
    background-color: %(ACCENT)s;
    border-radius: 6px;
}
.jg-root scale slider {
    background-color: %(TEXT2)s;
    border-radius: 50%%;
    min-width: 14px;
    min-height: 14px;
}

/* ------------------------------------------------------------ checkbuttons */
.jg-root checkbutton check {
    background-color: %(BG_CANVAS)s;
    border: 1px solid %(BORDER2)s;
    border-radius: 5px;
    min-width: 16px;
    min-height: 16px;
}
.jg-root checkbutton check:checked {
    background-color: %(BLUE_LT)s;
    border-color: %(BLUE_LT)s;
    color: #ffffff;
}

/* ------------------------------------------------------------ treeview (error log) */
.jg-root treeview.view {
    background-color: %(BG_CANVAS)s;
    color: %(TEXT)s;
}
.jg-root treeview.view:selected { background-color: %(ACCENT)s; color: #ffffff; }
.jg-root treeview.view header button {
    background-image: none;
    background-color: %(PANEL)s;
    color: %(TEXTDIM)s;
    border: none;
    border-bottom: 1px solid %(BORDER)s;
    border-radius: 0;
    font-weight: bold;
    font-size: 9pt;
}

/* ------------------------------------------------------------ log + readouts */
.jg-root .jg-log text, .jg-root .jg-log {
    font-family: %(FONT_MONO)s;
    font-size: 9pt;
    background-color: %(BG_CANVAS)s;
    color: %(TEXT2)s;
}
.jg-root .jg-pos {
    font-family: %(FONT_MONO)s;
    font-size: 9pt;
    color: %(TEXTDIM)s;
}

/* Spinner + caption overlay over the preview. */
.jg-root .jg-spin {
    background-color: rgba(5, 7, 10, 0.72);
    border-radius: 12px;
    padding: 18px 22px;
    color: #ffffff;
}
.jg-root .jg-spin label { color: #ffffff; }

/* Work-area pill (top-left of the preview). */
.jg-root .jg-docsize {
    background-color: rgba(13, 17, 23, 0.85);
    border: 1px solid %(BORDER)s;
    border-radius: 8px;
    padding: 4px 9px;
    color: %(TEXTDIM)s;
    font-family: %(FONT_MONO)s;
    font-size: 9pt;
}

/* Machine / mode status pills (top-right of the preview). */
.jg-root .jg-status-profile, .jg-root .jg-status-mode {
    border-radius: 7px;
    padding: 3px 10px;
    color: #ffffff;
    font-size: 9pt;
    font-weight: bold;
}
.jg-root .jg-status-profile { background-color: %(GREEN_DARK)s; }
.jg-root .jg-status-mode { background-color: %(BLUE)s; }

/* Red unseen-error count badge on the Error Log tab. */
.jg-root .jg-badge {
    background-color: #f85149;
    color: #ffffff;
    font-size: 8pt;
    font-weight: bold;
    border-radius: 9px;
    padding: 0 6px;
}

/* Layer-table header row. */
.jg-root .jg-th {
    background-color: %(PANEL)s;
    color: %(TEXTDIM)s;
    font-weight: bold;
    font-size: 9pt;
    padding: 6px 8px;
}

/* ------------------------------------------------------------ in-content top bar
   (a strip inside the window; the real OS title bar + controls stay native). */
.jg-root .jg-titlebar {
    background-image: linear-gradient(to bottom, %(PANEL)s, #121821);
    background-color: %(PANEL)s;
    border-bottom: 1px solid %(BORDER)s;
}
.jg-root .jg-titlebar .jg-title { color: %(TEXT2)s; font-weight: bold; }
/* Connection status pill (right of the top bar). */
.jg-root .jg-titlebar .jg-conn {
    background-color: %(BG_CANVAS)s;
    border: 1px solid %(BORDER)s;
    border-radius: 999px;
    padding: 3px 11px;
    color: %(TEXTDIM)s;
    font-size: 9pt;
    font-weight: bold;
}
.jg-conn-dot { min-width: 8px; min-height: 8px; border-radius: 50%%; }
.jg-conn-dot.on { background-color: %(GREEN)s; }
.jg-conn-dot.off { background-color: %(TEXTFAINT)s; }

/* Machine (Vinyl) / mode (Plotter) badge variants; defined AFTER the base pills so
   the class toggled on in _update_status_labels wins. */
.jg-root .jg-status-vinyl { background-color: %(VINYL)s; }
.jg-root .jg-status-plotter { background-color: %(PURPLE)s; }

/* Big Jog-Feed readout in the jog card. */
.jg-root .jg-jogfeed { font-family: %(FONT_MONO)s; font-size: 15pt; color: %(TEXT)s; }

/* Uppercase-ish card section headers (frame labels). */
.jg-root frame > label.jg-cardhead { color: %(TEXTFAINT)s; font-weight: bold; font-size: 8pt; }

/* Live X/Y position pill at the bottom-right of the preview. */
.jg-root .jg-canvas-pos {
    background-color: rgba(13, 17, 23, 0.85);
    border: 1px solid %(BORDER)s;
    border-radius: 6px;
    padding: 3px 9px;
    color: %(TEXTDIM)s;
    font-family: %(FONT_MONO)s;
    font-size: 9pt;
}

/* Error-log empty state. */
.jg-root .jg-empty { color: %(TEXTFAINTEST)s; font-size: 10pt; }

/* ------------------------------------------------------------ scrollbars */
.jg-root scrollbar { background-color: transparent; border: none; }
.jg-root scrollbar slider {
    background-color: %(BORDER2)s;
    border-radius: 6px;
    min-width: 8px;
    min-height: 8px;
}
.jg-root scrollbar slider:hover { background-color: #3a4351; }

/* Paned divider: faint strip with a 3-dot resize grip. */
.jg-root paned > separator {
    min-width: 5px;
    background-color: %(BORDER_FAINT)s;
    background-image:
        radial-gradient(circle, %(TEXTFAINT)s 1.1px, transparent 1.4px),
        radial-gradient(circle, %(TEXTFAINT)s 1.1px, transparent 1.4px),
        radial-gradient(circle, %(TEXTFAINT)s 1.1px, transparent 1.4px);
    background-repeat: no-repeat;
    background-position: center calc(50%% - 6px), center center, center calc(50%% + 6px);
}
""" % {
        "ACCENT": ACCENT, "ACCENT_DARK": ACCENT_DARK, "BG": BG, "BG_CANVAS": BG_CANVAS,
        "PANEL": PANEL, "CARD": CARD, "BORDER": BORDER, "BORDER2": BORDER2,
        "BORDER_FAINT": BORDER_FAINT, "TEXT": TEXT, "TEXT2": TEXT2, "TEXTDIM": TEXTDIM,
        "TEXTFAINT": TEXTFAINT, "TEXTFAINTEST": TEXTFAINTEST, "GREEN": GREEN,
        "GREEN_DARK": GREEN_DARK, "RED": RED, "RED_DARK": RED_DARK, "BLUE": BLUE,
        "BLUE_LT": BLUE_LT, "PURPLE": PURPLE, "VINYL": VINYL,
        "FONT_UI": FONT_UI, "FONT_MONO": FONT_MONO,
    }).encode("utf-8")


_applied = False


def apply_theme() -> None:
    """Force the dark theme preference for this process.

    The design is committed to a dark surface, so (unlike the previous
    Inkscape-mirroring behaviour) we always request the dark variant and then paint
    the exact palette on top via CSS. A dark base theme keeps GTK's own dialogs
    (file chooser, alerts) legible against our dark window.
    """
    settings = Gtk.Settings.get_default()
    if settings is None:
        return
    try:
        settings.set_property("gtk-application-prefer-dark-theme", True)
    except Exception:
        pass


def apply_css() -> None:
    global _applied
    apply_theme()
    if _applied:
        return
    provider = Gtk.CssProvider()
    try:
        provider.load_from_data(_build_css())
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    except Exception as exc:  # noqa: BLE001 - never let styling break the dialog
        # Individual unsupported CSS declarations are non-fatal (GTK logs and skips
        # them); this guards only the rare hard parse failure so the extension still
        # opens (with the dark theme preference applied) instead of erroring out.
        import sys
        print("jasGrbl: could not apply custom CSS: %s" % exc, file=sys.stderr)
    _applied = True
