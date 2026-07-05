#!/usr/bin/env python3
"""jasGrbl - "Author" menu entry.

Registered under Extensions > jas GRBL > Author. Shows a small alert dialog with
the application name, version, author and website. Uses Inkscape's bundled GTK 3
for a proper info dialog and falls back to inkex.errormsg() if GTK is unavailable.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import inkex  # noqa: E402

from jasgrbl_pkg import (  # noqa: E402
    __app_name__,
    __author__,
    __version__,
    __website__,
)


class JasGrblAuthor(inkex.EffectExtension):
    def effect(self):
        title = "About %s" % __app_name__
        details = (
            "App: %s\n"
            "Version: %s\n"
            "Author: %s\n"
            "Website: %s"
        ) % (__app_name__, __version__, __author__, __website__)

        try:
            import gi
            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk

            dialog = Gtk.MessageDialog(
                transient_for=None,
                modal=True,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text=title,
            )
            dialog.format_secondary_text(details)
            dialog.set_title(title)
            dialog.run()
            dialog.destroy()
        except Exception:
            # GTK missing / headless - fall back to Inkscape's own message dialog.
            inkex.errormsg("%s\n\n%s" % (title, details))


if __name__ == "__main__":
    JasGrblAuthor().run()
