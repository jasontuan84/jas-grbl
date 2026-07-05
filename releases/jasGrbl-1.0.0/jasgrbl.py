#!/usr/bin/env python3
"""jasGrbl - Inkscape effect extension entry point.

Registered under Extensions > jas GRBL. Instead of the auto-generated parameter
dialog, this effect launches a custom GTK 3 window (jasgrbl_pkg.ui.main_window)
that hosts the full UI: GRBL Connect, Serial Log, and Gcode Generate.
"""

import os
import sys

# Make the bundled package importable regardless of where Inkscape mounts us.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import inkex  # noqa: E402


class JasGrbl(inkex.EffectExtension):
    def effect(self):
        try:
            from jasgrbl_pkg.ui.main_window import launch
        except Exception as exc:  # GTK missing or import error - report cleanly
            inkex.errormsg(
                "jasGrbl could not start its interface: %s\n\n"
                "This extension needs Inkscape's bundled GTK 3 / PyGObject." % exc)
            return
        # launch() builds the window and runs Gtk.main(); on close, the (possibly
        # modified) document is written back to Inkscape by the inkex framework.
        launch(self.svg)


if __name__ == "__main__":
    JasGrbl().run()
