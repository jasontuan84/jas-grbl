"""Compatibility shims applied before any ``gi.repository`` GTK import.

Inkscape 1.4.x on macOS ships PyGObject 3.50.x against a GLib < 2.86 whose
``GioUnix`` typelib does NOT expose ``DesktopAppInfo``. PyGObject's
``gi/overrides/GioUnix.py`` unconditionally does ``class DesktopAppInfo(
GioUnix.DesktopAppInfo)`` for GLib < 2.86, so merely importing that override
raises::

    AttributeError: 'gi.repository.GioUnix' object has no attribute 'DesktopAppInfo'

That override is pulled in transitively the first time ``Gtk`` (hence ``Gio``)
is imported -- ``gi/overrides/Gio.py`` does ``from gi.repository import
GioUnix`` (wrapped in ``suppress(ImportError)``). By marking the GioUnix /
GioWin32 submodules as unimportable *before* the first Gtk import, that
``from ... import`` raises ``ImportError`` instead of ``AttributeError`` and is
swallowed cleanly. The Unix-only platform symbols it would have aliased into
Gio (unix mounts, fd-based streams) are unused by this dialog.

Setting ``sys.modules[name] = None`` makes ``import name`` raise ``ImportError``
-- a documented CPython import behaviour, checked before any meta-path finder
runs, so it is independent of PyGObject's dynamic importer.
"""

from __future__ import annotations

import sys


def _block_broken_gio_platform_overrides() -> None:
    for name in ("gi.repository.GioUnix", "gi.repository.GioWin32"):
        # Don't clobber a module that already imported successfully.
        if sys.modules.get(name) is None:
            sys.modules[name] = None


_block_broken_gio_platform_overrides()
