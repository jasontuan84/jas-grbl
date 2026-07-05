"""Shared constants, enums and defaults for jasGrbl."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Home / origin corner of the machine work area.
# Values are stable strings used in config + the home-position combo box.
# ---------------------------------------------------------------------------
HOME_TOP_LEFT = "top-left"
HOME_TOP_RIGHT = "top-right"
HOME_BOTTOM_LEFT = "bottom-left"
HOME_BOTTOM_RIGHT = "bottom-right"

HOME_POSITIONS = [
    HOME_BOTTOM_LEFT,
    HOME_BOTTOM_RIGHT,
    HOME_TOP_LEFT,
    HOME_TOP_RIGHT,
]

# ---------------------------------------------------------------------------
# Fill strategy identifiers (also used as labels, title-cased in the UI).
# ---------------------------------------------------------------------------
FILL_AUTO = "auto"            # meta: pick the best strategy per shape
FILL_HATCH = "hatch"
FILL_CROSSHATCH = "crosshatch"
FILL_ZIGZAG = "zigzag"
FILL_CONTOUR = "contour"
FILL_SPIRAL = "spiral"
FILL_HILBERT = "hilbert"
FILL_PEANO = "peano"
FILL_VORONOI = "voronoi"

FILL_TYPES = [
    FILL_AUTO,
    FILL_HATCH,
    FILL_CROSSHATCH,
    FILL_ZIGZAG,
    FILL_CONTOUR,
    FILL_SPIRAL,
    FILL_HILBERT,
    FILL_PEANO,
    FILL_VORONOI,
]

FILL_LABELS = {
    FILL_AUTO: "Auto",
    FILL_HATCH: "Hatch",
    FILL_CROSSHATCH: "Cross-Hatch",
    FILL_ZIGZAG: "Zigzag",
    FILL_CONTOUR: "Contour",
    FILL_SPIRAL: "Spiral",
    FILL_HILBERT: "Hilbert",
    FILL_PEANO: "Peano",
    FILL_VORONOI: "Voronoi",
}

# ---------------------------------------------------------------------------
# Output mode: laser engraving vs pen/knife plotter.
# Engraving drives the laser (M3/M4 S..); Plotter raises/lowers a servo pen.
# The mode changes both the per-layer settings shown and the emitted code, so
# switching it invalidates any generated output (the UI clears it).
# ---------------------------------------------------------------------------
MODE_ENGRAVING = "engraving"
MODE_PLOTTER = "plotter"
MODES = [MODE_ENGRAVING, MODE_PLOTTER]
MODE_LABELS = {MODE_ENGRAVING: "Engraving", MODE_PLOTTER: "Plotter"}
DEFAULT_MODE = MODE_ENGRAVING

# ---------------------------------------------------------------------------
# Machine profile: selects the target machine's Home corner and output format.
# A GRBL laser/CNC homes Bottom-Left and exports G-code; a vinyl cutter homes
# Top-Right and exports HPGL. Home defines the whole coordinate system (see
# docs/knowledge/basic/machine-home-position-coordinate-system.md), so switching
# profile re-maps the axes and invalidates any generated output.
# ---------------------------------------------------------------------------
PROFILE_GRBL = "grbl"
PROFILE_VINYL = "vinyl"
PROFILES = [PROFILE_GRBL, PROFILE_VINYL]
PROFILE_LABELS = {PROFILE_GRBL: "GRBL", PROFILE_VINYL: "Vinyl Cutter"}
# Home corner implied by each profile (per the coordinate-system knowledge doc).
PROFILE_HOME = {PROFILE_GRBL: HOME_BOTTOM_LEFT, PROFILE_VINYL: HOME_TOP_LEFT}
DEFAULT_PROFILE = PROFILE_GRBL

# Plotter per-layer defaults.
DEFAULT_PLOTTER_FORCE = 80     # grams (pen/knife pressure -> HPGL FS)
DEFAULT_PLOTTER_SPEED = 250    # mm/s  (-> GRBL feed *60, HPGL VS /10 as cm/s)

# GRBL servo pen control (Plotter mode): pen DOWN turns the spindle output on so a
# servo swings down; pen UP turns it off. A short dwell lets the servo settle.
PEN_DOWN_CMD = "M3"
PEN_UP_CMD = "M5"
PEN_SETTLE_S = 0.2            # G4 dwell seconds after each pen up/down

# HPGL: 1 plotter unit = 0.025 mm (40 units per mm). Standard file extension .plt.
HPGL_UNITS_PER_MM = 40.0
HPGL_EXT = ".plt"

# ---------------------------------------------------------------------------
# Laser power mode.
# ---------------------------------------------------------------------------
LASER_DYNAMIC = "M4"   # dynamic power, scales with speed (recommended for engraving)
LASER_CONSTANT = "M3"  # constant power

# ---------------------------------------------------------------------------
# Defaults.
# ---------------------------------------------------------------------------
DEFAULT_BAUD = 115200
DEFAULT_VINYL_BAUD = 9600      # vinyl cutters (e.g. Refine LH721) commonly default to 9600
BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 250000]

# ---------------------------------------------------------------------------
# Serial flow control. HPGL vinyl cutters send no per-command ACK (unlike GRBL's
# "ok"), so the host must not outrun the cutter's receive buffer. Flow control lets
# the cutter throttle us: it raises XOFF / drops CTS when its buffer is full and the
# OS driver then blocks our write() until it clears. Software (XON/XOFF) is the most
# widely compatible default for HPGL cutters. GRBL uses NONE (it paces via the ACK).
# ---------------------------------------------------------------------------
FLOW_SOFTWARE = "software"     # XON/XOFF (0x11/0x13)
FLOW_HARDWARE = "hardware"     # RTS/CTS
FLOW_NONE = "none"
FLOW_CONTROLS = [FLOW_SOFTWARE, FLOW_HARDWARE, FLOW_NONE]
FLOW_LABELS = {
    FLOW_SOFTWARE: "Software (XON/XOFF)",
    FLOW_HARDWARE: "Hardware (RTS/CTS)",
    FLOW_NONE: "None",
}
DEFAULT_FLOW = FLOW_SOFTWARE

# Paced sending (HPGL, no ACK): write the plot in chunks with an optional delay between
# them. With flow control on, write() already blocks when throttled, so the delay is a
# belt-and-braces pause that matters mostly when flow control is None.
DEFAULT_SEND_CHUNK = 512       # bytes per write
DEFAULT_SEND_DELAY_MS = 0      # pause between chunks (ms)

# Fallback work area, used only when the Inkscape document size cannot be read.
DEFAULT_BOARD_WIDTH_MM = 400.0
DEFAULT_BOARD_HEIGHT_MM = 400.0
DEFAULT_S_MAX = 1000          # GRBL $30 (max spindle/laser value)

DEFAULT_POWER = 80            # %
DEFAULT_SPEED = 1000          # mm/min
DEFAULT_PASSES = 1

DEFAULT_FILL_ANGLE = 45.0     # degrees
DEFAULT_FILL_SPACING = 0.30   # mm - the spacing for a FULLY DARK (black) fill
DEFAULT_FLATNESS_MM = 0.05    # bezier flattening tolerance in mm (finer = smoother curves)

# Shade-by-colour density (doc 03 s2): darker fill => denser lines, lighter => sparser.
# fill_spacing is the black/darkest spacing; a lighter colour multiplies it up to this
# factor (a light grey ends up ~5x sparser, matching the doc's representative shade map).
DEFAULT_SHADE_DENSITY = True
SHADE_MAX_MULT = 5.0

# ---------------------------------------------------------------------------
# Serial Log colors (RGBA hex used as GTK TextTag foreground).
# ---------------------------------------------------------------------------
# Vibrant per-actor palette for the Serial Log. Only the short "[ACTOR]" token is
# tagged with these (the message body keeps the theme's default text colour), so we
# can use loud, mid-tone hues that stay legible on both light and dark themes.
LOG_COLORS = {
    "GRBL": "#56d4dd",    # cyan - firmware banner
    "TX": "#58a6ff",      # blue - bytes sent
    "RX": "#3fb950",      # green - raw received
    "OK": "#3fb950",      # green - acknowledged
    "ERROR": "#f85149",   # red - error
    "ALARM": "#ff2d78",   # magenta - alarm (distinct from error red)
    "INFO": "#8b949e",    # grey - status
    "WARN": "#e3a008",    # amber - warning
}
# Timestamp colour for each log line (faint, matching the design's serial rows).
LOG_META_COLOR = "#5c6672"

# Direction glyph shown before the actor token: sent (»), received («), local (•).
LOG_ARROWS = {
    "TX": "»",
    "RX": "«", "OK": "«", "GRBL": "«",
    "INFO": "•", "WARN": "•", "ERROR": "•", "ALARM": "•",
}

# Window
WINDOW_TITLE = "jas GRBL"
WINDOW_MIN_WIDTH = 1000
