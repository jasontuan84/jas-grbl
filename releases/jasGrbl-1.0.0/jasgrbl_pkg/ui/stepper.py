"""A compact ``[ - ][ value ][ + ]`` numeric stepper (from the design mock).

Matches the design's inline steppers while staying keyboard-editable: the centre is a
real ``Gtk.Entry`` (so a value can be typed) flanked by minus/plus buttons. It exposes
the slice of the ``Gtk.SpinButton`` API the rest of the UI relies on
(``get_value`` / ``set_value`` / ``get_value_as_int`` / ``set_digits``) plus a live
``.adjustment`` so existing call sites that read the adjustment keep working.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GObject, Gtk  # noqa: E402


class Stepper(Gtk.Box):
    __gsignals__ = {
        # Emitted whenever the committed value changes (button or typed + commit).
        "value-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, value, lower, upper, step, digits=0, width_chars=4):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.get_style_context().add_class("jg-stepper")
        self._digits = digits
        self._step = step
        # Page increment (10 steps) keeps the API shape of a real Adjustment.
        self.adjustment = Gtk.Adjustment(
            value=value, lower=lower, upper=upper,
            step_increment=step, page_increment=step * 10, page_size=0)

        btn_down = Gtk.Button(label="−")   # minus sign
        btn_down.set_relief(Gtk.ReliefStyle.NONE)
        btn_down.connect("clicked", lambda *_: self._bump(-1))

        self._entry = Gtk.Entry()
        self._entry.set_width_chars(width_chars)
        self._entry.set_max_width_chars(width_chars)
        self._entry.set_alignment(0.5)
        self._entry.set_has_frame(False)
        self._entry.set_input_purpose(Gtk.InputPurpose.NUMBER)
        self._entry.connect("activate", lambda *_: self._commit_entry())
        self._entry.connect("focus-out-event", self._on_focus_out)

        btn_up = Gtk.Button(label="+")
        btn_up.set_relief(Gtk.ReliefStyle.NONE)
        btn_up.connect("clicked", lambda *_: self._bump(1))

        self.pack_start(btn_down, False, False, 0)
        self.pack_start(self._entry, True, True, 0)
        self.pack_start(btn_up, False, False, 0)

        self._refresh_entry()

    # ------------------------------------------------------------------ helpers
    def _clamp(self, v: float) -> float:
        return max(self.adjustment.get_lower(), min(self.adjustment.get_upper(), v))

    def _format(self, v: float) -> str:
        return ("%.*f" % (self._digits, v)) if self._digits else "%d" % int(round(v))

    def _refresh_entry(self) -> None:
        self._entry.set_text(self._format(self.adjustment.get_value()))

    def _set_value(self, v: float, notify: bool = True) -> None:
        v = self._clamp(round(v, self._digits) if self._digits else round(v))
        changed = v != self.adjustment.get_value()
        self.adjustment.set_value(v)
        self._refresh_entry()
        if changed and notify:
            self.emit("value-changed")

    def _bump(self, direction: int) -> None:
        self._set_value(self.adjustment.get_value() + direction * self._step)

    def _commit_entry(self) -> None:
        try:
            v = float(self._entry.get_text().replace(",", ".").strip())
        except (ValueError, TypeError):
            self._refresh_entry()           # revert bad input
            return
        self._set_value(v)

    def _on_focus_out(self, *_):
        self._commit_entry()
        return False

    # ------------------------------------------------ SpinButton-compatible API
    def get_value(self) -> float:
        # Commit any pending typed text so the returned value is always current.
        self._commit_entry()
        return self.adjustment.get_value()

    def get_value_as_int(self) -> int:
        return int(round(self.get_value()))

    def set_value(self, v: float) -> None:
        self._set_value(v, notify=False)

    def set_digits(self, digits: int) -> None:
        self._digits = digits
        self._refresh_entry()
