#!/usr/bin/env bash
# Dev install for jasGrbl (Linux/macOS). Symlinks src/ into the Inkscape user
# extensions dir so code changes are picked up by re-running the extension.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"

case "$(uname -s)" in
    Darwin)
        DST="$HOME/Library/Application Support/org.inkscape.Inkscape/config/inkscape/extensions/jasGrbl" ;;
    *)
        DST="${XDG_CONFIG_HOME:-$HOME/.config}/inkscape/extensions/jasGrbl" ;;
esac

mkdir -p "$(dirname "$DST")"
rm -rf "$DST"
ln -s "$SRC" "$DST"

echo "Linked:"
echo "  $DST  ->  $SRC"
echo
echo "Restart Inkscape, then open: Extensions > jas GRBL"
echo "After editing .py files just re-run the extension; after editing .inx, restart Inkscape."
