"""Machine + extension configuration, persisted as JSON in the user config dir."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

from . import constants as C


def _config_dir() -> str:
    """Return (and create) the directory where jasGrbl stores its settings.

    Mirrors Inkscape's per-user config location but keeps a dedicated subfolder
    so the file is easy to find and never clobbers Inkscape's own preferences.
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "inkscape", "jasGrbl")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        path = os.path.join(base, "inkscape", "jasGrbl")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        path = os.path.expanduser("~")
    return path


CONFIG_PATH = os.path.join(_config_dir(), "config.json")


def inkscape_pref_path() -> str:
    """Path to Inkscape's own preferences.xml (sibling of our config folder)."""
    return os.path.join(os.path.dirname(_config_dir()), "preferences.xml")


def read_inkscape_theme():
    """Read Inkscape's GTK theme choice so our dialog can match it.

    Returns (gtk_theme_name_or_None, prefer_dark_or_None) from the <group id="theme">
    element of preferences.xml. Either value is None when unknown."""
    try:
        import xml.etree.ElementTree as ET
        root = ET.parse(inkscape_pref_path()).getroot()
        for el in root.iter():
            if el.get("id") == "theme":
                theme = el.get("gtkTheme") or None
                pd = el.get("preferDarkTheme")
                dark = None if pd is None else pd.strip() in ("1", "true", "True")
                return theme, dark
    except Exception:
        pass
    return None, None


@dataclass
class LayerSetting:
    """Per-layer laser settings, keyed by the layer's SVG id in the config."""

    enabled: bool = True
    # Engraving (laser) settings.
    power: int = C.DEFAULT_POWER          # 0..100 (%)
    speed: int = C.DEFAULT_SPEED          # mm/min
    passes: int = C.DEFAULT_PASSES        # >= 1
    # Plotter (pen/knife) settings.
    force: int = C.DEFAULT_PLOTTER_FORCE          # grams
    plotter_speed: int = C.DEFAULT_PLOTTER_SPEED  # mm/s
    stroke_text: bool = False


@dataclass
class MachineConfig:
    """Everything persisted between sessions."""

    port: str = ""
    baud: int = C.DEFAULT_BAUD
    # Machine profile selects the target machine. It drives the Home corner and the
    # output format (GRBL -> G-code, Vinyl Cutter -> HPGL); see PROFILE_HOME.
    profile: str = C.DEFAULT_PROFILE
    # Work area follows the Inkscape document size; the design keeps its document
    # position. Home is derived from the profile (PROFILE_HOME), not set directly.
    home: str = C.HOME_BOTTOM_LEFT
    s_max: int = C.DEFAULT_S_MAX
    laser_mode: str = C.LASER_DYNAMIC
    mode: str = C.DEFAULT_MODE            # engraving | plotter

    # Vinyl-cutter Cut Setting (VINYL tab): knife speed (mm/s) and force (g).
    cut_speed: int = C.DEFAULT_PLOTTER_SPEED     # mm/s (-> HPGL VS, cm/s)
    cut_force: int = C.DEFAULT_PLOTTER_FORCE     # grams (-> HPGL FS)

    # Vinyl-cutter serial connection technique (HPGL has no per-line ACK; see constants).
    vinyl_port: str = ""                         # remembered separately from the GRBL port
    vinyl_baud: int = C.DEFAULT_VINYL_BAUD       # remembered separately from the GRBL baud
    flow_control: str = C.DEFAULT_FLOW           # software | hardware | none
    send_chunk: int = C.DEFAULT_SEND_CHUNK       # bytes per write when streaming HPGL
    send_delay_ms: int = C.DEFAULT_SEND_DELAY_MS # pause between chunks (ms)
    vinyl_swap_xy: bool = False                  # swap X/Y in all HPGL out (jog + cut) for
                                                 # cutters whose axes are transposed (LH721)

    # Generation defaults
    fill_type: str = C.FILL_AUTO
    fill_angle: float = C.DEFAULT_FILL_ANGLE
    fill_spacing: float = C.DEFAULT_FILL_SPACING     # spacing for the darkest fill
    flatness_mm: float = C.DEFAULT_FLATNESS_MM
    shade_density: bool = C.DEFAULT_SHADE_DENSITY    # darker fill colour => denser lines

    # Per-layer settings, keyed by layer id -> LayerSetting (dict form on disk)
    layers: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ I/O
    @classmethod
    def load(cls) -> "MachineConfig":
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return cls()
        cfg = cls()
        for key, value in data.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        # Normalise layers into LayerSetting-shaped dicts
        if not isinstance(cfg.layers, dict):
            cfg.layers = {}
        return cfg

    def save(self) -> None:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(asdict(self), fh, indent=2)
        except OSError:
            pass  # never let a config write failure break the dialog

    # -------------------------------------------------------------- layers
    def layer_setting(self, layer_id: str) -> LayerSetting:
        raw = self.layers.get(layer_id)
        if isinstance(raw, dict):
            ls = LayerSetting()
            for key, value in raw.items():
                if hasattr(ls, key):
                    setattr(ls, key, value)
            return ls
        return LayerSetting()

    def set_layer_setting(self, layer_id: str, setting: LayerSetting) -> None:
        self.layers[layer_id] = asdict(setting)
