#!/usr/bin/env python3
"""Package jasGrbl into a release zip under ../../releases/.

Usage:  python tools/package.py
Produces:  releases/jasGrbl-<version>/   (unzipped tree)
           releases/jasGrbl-<version>.zip
"""

import os
import shutil
import sys
import zipfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))           # repo root (holds jasgrbl.inx)
RELEASES = os.path.join(SRC, "releases")

# Files/dirs shipped in the extension bundle (relative to src/).
INCLUDE = ["jasgrbl.inx", "jasgrbl.py",
           "jasgrbl_author.inx", "jasgrbl_author.py", "jasgrbl_pkg"]
EXCLUDE_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache"}
EXCLUDE_SUFFIX = (".pyc", ".pyo")


def _version() -> str:
    sys.path.insert(0, SRC)
    from jasgrbl_pkg import __version__
    return __version__


def _copy(src, dst):
    if os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            if name in EXCLUDE_NAMES:
                continue
            _copy(os.path.join(src, name), os.path.join(dst, name))
    else:
        if src.endswith(EXCLUDE_SUFFIX):
            return
        shutil.copy2(src, dst)


def main():
    version = _version()
    name = "jasGrbl-%s" % version
    out_dir = os.path.join(RELEASES, name)
    zip_path = os.path.join(RELEASES, name + ".zip")

    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    for item in INCLUDE:
        _copy(os.path.join(SRC, item), os.path.join(out_dir, item))

    # User-facing install note inside the bundle.
    with open(os.path.join(out_dir, "INSTALL.txt"), "w", encoding="utf-8") as fh:
        fh.write(
            "jasGrbl %s\n"
            "================\n\n"
            "There are two ways to install. Method A is easiest if you have\n"
            "Inkscape 1.2 or newer; Method B works on every Inkscape version.\n\n"
            "-----------------------------------------------------------------\n"
            "METHOD A - Inkscape Extension Manager (Install Package)\n"
            "-----------------------------------------------------------------\n"
            "Requires Inkscape 1.2+ (has the extension manager).\n\n"
            "1. In Inkscape, open:  Extensions > Manage Extensions\n"
            "2. Go to the Install Package section and click the folder button\n"
            "   to choose a local file.\n"
            "3. Select the file:  jasGrbl-%s.zip\n"
            "4. Restart Inkscape.\n\n"
            "Note: install the ZIP itself, not this unpacked folder.\n\n"
            "-----------------------------------------------------------------\n"
            "METHOD B - Manual copy (any Inkscape version)\n"
            "-----------------------------------------------------------------\n"
            "1. Copy this folder's CONTENTS (jasgrbl.inx, jasgrbl.py,\n"
            "   jasgrbl_pkg/, ...) into your Inkscape user extensions dir:\n"
            "     Windows: %%APPDATA%%\\inkscape\\extensions\n"
            "     macOS:   ~/Library/Application Support/org.inkscape.Inkscape/"
            "config/inkscape/extensions\n"
            "     Linux:   ~/.config/inkscape/extensions\n"
            "   (Exact path: Edit > Preferences > System > User extensions.)\n"
            "2. Restart Inkscape.\n\n"
            "-----------------------------------------------------------------\n"
            "AFTER INSTALLING\n"
            "-----------------------------------------------------------------\n"
            "Open the extension from:  Extensions > jas GRBL\n\n"
            "Optional - USB streaming needs pyserial in Inkscape's Python:\n"
            "   pip install pyserial\n"
            "The extension loads and generates G-code fine without it; only\n"
            "the USB streaming feature requires pyserial.\n" % (version, version))

    if os.path.exists(zip_path):
        os.remove(zip_path)
    # Zip with files at the archive ROOT (jasgrbl.inx, jasgrbl.py, jasgrbl_pkg/...).
    # Inkscape's Extension Manager expects the .inx at the top level of the archive;
    # a wrapping folder makes "Install from file" fail to recognize it.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, _dirs, files in os.walk(out_dir):
            for f in files:
                full = os.path.join(base, f)
                arc = os.path.relpath(full, out_dir)
                zf.write(full, arc)

    print("Packaged %s" % zip_path)


if __name__ == "__main__":
    main()
