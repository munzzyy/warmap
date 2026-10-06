#!/usr/bin/env bash
# Install warmap as a real app on this machine:
#   - a `warmap` command on your PATH (~/.local/bin)
#   - an app-menu entry that launches the map window
#
# The command is a symlink back into this repo rather than a copy, so the
# bundled identifier registries under warmap/data/ and the sample session
# under warmap/sample/ stay available to the installed app.
#
# Nothing here touches the network or needs root. warmap's own data (the
# collected-AP store, the tile cache) lives under ~/.local/share/warmap/;
# the only network calls the app itself ever makes are map-tile fetches to
# OpenStreetMap, and those are cached after the first time. Re-runnable
# (idempotent). No autostart, nothing here needs to run unattended. Undo
# notes at the bottom.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS_SVG="$HOME/.local/share/icons/hicolor/scalable/apps"
ICONS_PNG="$HOME/.local/share/icons/hicolor/256x256/apps"

echo ">> warmap install (repo: $REPO)"

mkdir -p "$BIN" "$APPS" "$ICONS_SVG" "$ICONS_PNG"

# 1) `warmap` on PATH
ln -sf "$REPO/bin/warmap" "$BIN/warmap"
chmod +x "$REPO/bin/warmap"
echo "   - command:  $BIN/warmap  (make sure ~/.local/bin is on your PATH)"

# 2) icon (svg + png, see tools/gen_icon.py)
cp -f "$REPO/bin/warmap.svg" "$ICONS_SVG/warmap.svg"
cp -f "$REPO/bin/warmap.png" "$ICONS_PNG/warmap.png"
echo "   - icon: $ICONS_SVG/warmap.svg, $ICONS_PNG/warmap.png"

# 3) app-menu entry: one click opens the map window
cat > "$APPS/warmap.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=warmap
GenericName=Wardriving Map Viewer
Comment=Map everything you capture: Wi-Fi, Bluetooth, Sub-GHz, NFC, RFID, iButton and IR
Exec=/usr/bin/env python3 $REPO/bin/warmap
Icon=warmap
Terminal=false
StartupWMClass=warmap
Categories=Utility;Network;
Keywords=wardriving;wifi;bluetooth;ble;marauder;flipper;subghz;nfc;rfid;wigle;map;
EOF
echo "   - menu entry: warmap (native app, one click)"

update-desktop-database "$APPS" >/dev/null 2>&1 || true
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo
echo "Done. Try it now:"
echo "  warmap                              # opens the map window"
echo "  warmap open ~/Downloads/captures/     # load a file or a whole folder"
echo "  warmap import-sd                      # scan removable media for captures"
echo "  warmap formats                        # list every format it reads"
echo "  (or click warmap in your app menu)"
echo
echo "UNDO:"
echo "  rm -f $BIN/warmap $APPS/warmap.desktop $ICONS_SVG/warmap.svg $ICONS_PNG/warmap.png"
echo "  rm -rf ~/.local/share/warmap   # drops the collected-AP store + tile cache"
echo "  rm -rf ~/.config/warmap        # drops the remembered window geometry"
