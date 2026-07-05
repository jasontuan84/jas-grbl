"""Colorized Serial Log: [DateTime] [Actor] [Message]."""

from __future__ import annotations

from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # noqa: E402

from .. import constants as C


class LogView(Gtk.Box):
    """Read-only, auto-scrolling, color-tagged text log.

    ``append`` must be called on the GTK main thread. Background threads should
    marshal through GLib.idle_add (see main_window)."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.autoscroll = True

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)

        self.view = Gtk.TextView()
        self.view.set_editable(False)
        self.view.set_cursor_visible(False)
        self.view.set_monospace(True)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.get_style_context().add_class("jg-log")
        self.buffer = self.view.get_buffer()

        # timestamp/meta tag (violet, italic so it reads as secondary)
        self.buffer.create_tag(
            "meta", foreground=C.LOG_META_COLOR, style=Pango.Style.ITALIC)
        # Every actor token is bold + vibrant so it stands out from the message body.
        for actor, color in C.LOG_COLORS.items():
            self.buffer.create_tag(actor, foreground=color, weight=700)

        scrolled.add(self.view)
        self.pack_start(scrolled, True, True, 0)

    def append(self, actor: str, message: str) -> None:
        actor = actor if actor in C.LOG_COLORS else "INFO"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        arrow = C.LOG_ARROWS.get(actor, "•")
        end = self.buffer.get_end_iter()
        self.buffer.insert_with_tags_by_name(end, "[%s] " % ts, "meta")
        # Direction glyph (»/«/•) + actor token share the actor's vibrant colour.
        self.buffer.insert_with_tags_by_name(self.buffer.get_end_iter(), "%s " % arrow, actor)
        self.buffer.insert_with_tags_by_name(self.buffer.get_end_iter(), "[%s] " % actor, actor)
        self.buffer.insert(self.buffer.get_end_iter(), message + "\n")
        if self.autoscroll:
            mark = self.buffer.get_insert()
            self.buffer.place_cursor(self.buffer.get_end_iter())
            self.view.scroll_to_mark(self.buffer.get_insert(), 0.0, False, 0.0, 1.0)
            self.buffer.move_mark(mark, self.buffer.get_end_iter())

    def clear(self) -> None:
        self.buffer.set_text("")
