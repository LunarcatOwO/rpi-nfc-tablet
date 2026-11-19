#!/usr/bin/env bash
# rpi-nfc-tablet: touch NFC sign-in kiosk for a Raspberry Pi 4 B
# Created by LunarcatOwO (https://github.com/LunarcatOwO)
# Copyright (C) 2026 LunarcatOwO
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. It comes WITHOUT ANY WARRANTY; see the LICENSE file.

# Install the NFC Kiosk desktop app on the Raspberry Pi and start it at login.
#
#   bash electron/install-pi.sh [path/to/nfc-kiosk-*-arm64.deb]
#
# Run scripts/install.sh first (database, Python venv, config.toml).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# The .deb given, or else the newest one built in electron/dist/.
DEB="${1:-$(ls "$REPO_DIR"/electron/dist/nfc-kiosk-*-arm64.deb 2>/dev/null | tail -n1)}"
if [ -z "$DEB" ] || [ ! -f "$DEB" ]; then
    echo "No .deb found. Build it with: cd electron && npm install && npm run dist:pi" >&2
    echo "or pass the path to a downloaded nfc-kiosk-*-arm64.deb." >&2
    exit 1
fi

echo "==> Installing $DEB"
sudo apt-get install -y "$(realpath "$DEB")"

echo "==> Starting the app at login"
mkdir -p "$HOME/.config/autostart"
sed "s#^Exec=.*#Exec=nfc-kiosk --home $REPO_DIR#" "$REPO_DIR/electron/autostart/nfc-kiosk.desktop" \
    > "$HOME/.config/autostart/nfc-kiosk.desktop"

echo
echo "Done. Log out and back in (or reboot) and the kiosk opens fullscreen."
echo "Ctrl+Alt+Q quits it."
