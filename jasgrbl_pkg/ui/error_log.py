"""Error Log tab: a searchable, newest-first table of every error the extension hits
while running (DateTime, Actor, Message), with a Clear Log button.

Kept separate from the Serial Log (a GRBL-only byte trace); this collects errors from
generation, serial I/O and streaming across both machine profiles. The owning window
adds a red unseen-count badge to the tab label (see main_window)."""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # noqa: E402


class ErrorLogView(Gtk.Box):
    # Columns in the backing store.
    COL_TIME, COL_ACTOR, COL_MESSAGE = 0, 1, 2

    def __init__(self, on_change: Optional[Callable[[], None]] = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_border_width(8)
        self._on_change = on_change            # notified after add/clear (badge refresh)

        # Toolbar: keyword filter + Clear Log.
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Filter errors by keyword…")
        self.search.connect("search-changed", self._on_search_changed)
        btn_clear = Gtk.Button(label="Clear Log")
        btn_clear.set_tooltip_text("Remove all entries from the Error Log")
        btn_clear.connect("clicked", lambda *_: self.clear())
        toolbar.pack_start(self.search, True, True, 0)
        toolbar.pack_end(btn_clear, False, False, 0)
        self.pack_start(toolbar, False, False, 0)

        # Store + keyword filter. Newest row is inserted at the top (index 0).
        self.store = Gtk.ListStore(str, str, str)
        self._filter = self.store.filter_new()
        self._filter.set_visible_func(self._row_visible)
        self.tree = Gtk.TreeView(model=self._filter)
        self.tree.set_headers_visible(True)
        for idx, title, expand in (
                (self.COL_TIME, "Time", False),
                (self.COL_ACTOR, "Actor", False),
                (self.COL_MESSAGE, "Message", True)):
            renderer = Gtk.CellRendererText()
            if idx == self.COL_MESSAGE:
                renderer.set_property("wrap-mode", Pango.WrapMode.WORD_CHAR)
                renderer.set_property("wrap-width", 240)
            column = Gtk.TreeViewColumn(title, renderer, text=idx)
            column.set_resizable(True)
            if expand:
                column.set_expand(True)
            self.tree.append_column(column)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.add(self.tree)

        # Centred empty-state placeholder (design): a check icon + message shown over the
        # (empty) table when there are no rows, or no rows match the filter.
        overlay = Gtk.Overlay()
        overlay.add(scrolled)
        self._empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._empty.set_halign(Gtk.Align.CENTER)
        self._empty.set_valign(Gtk.Align.CENTER)
        self._empty.get_style_context().add_class("jg-empty")
        self._empty.pack_start(
            Gtk.Image.new_from_icon_name("emblem-ok-symbolic", Gtk.IconSize.DIALOG),
            False, False, 0)
        self._empty_label = Gtk.Label(label="No errors logged. Everything looks good.")
        self._empty.pack_start(self._empty_label, False, False, 0)
        overlay.add_overlay(self._empty)
        self.pack_start(overlay, True, True, 0)
        self._refresh_visibility()

    def _on_search_changed(self, *_):
        self._filter.refilter()
        self._refresh_visibility()

    def _refresh_visibility(self) -> None:
        """Show the empty-state placeholder when no rows are visible."""
        if not hasattr(self, "_empty"):
            return
        visible = self._filter.iter_n_children(None)
        if visible == 0:
            self._empty_label.set_text(
                "No errors logged. Everything looks good." if len(self.store) == 0
                else "No errors match your filter.")
            self._empty.show_all()
        else:
            self._empty.hide()

    def _row_visible(self, model, tree_iter, _data) -> bool:
        keyword = self.search.get_text().strip().lower()
        if not keyword:
            return True
        return any(keyword in (model.get_value(tree_iter, c) or "").lower()
                   for c in (self.COL_TIME, self.COL_ACTOR, self.COL_MESSAGE))

    def add(self, timestamp: str, actor: str, message: str) -> None:
        """Add one error at the top (newest first). Must run on the GTK main thread."""
        self.store.insert(0, [timestamp, actor, message])
        self._refresh_visibility()
        if self._on_change:
            self._on_change()

    def clear(self) -> None:
        self.store.clear()
        self._refresh_visibility()
        if self._on_change:
            self._on_change()

    def count(self) -> int:
        return len(self.store)
