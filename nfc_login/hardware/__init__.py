# rpi-nfc-tablet: touch NFC sign-in kiosk for a Raspberry Pi 4 B
# Created by LunarcatOwO (https://github.com/LunarcatOwO)
# Copyright (C) 2026 LunarcatOwO
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. It comes WITHOUT ANY WARRANTY; see the LICENSE file.

"""Hardware drivers: the PN532 NFC reader and a simulator."""

from __future__ import annotations


def create_reader(config):
    """The PN532 reader, or a simulated one when ``hardware.mode = "simulated"``."""
    if config.hardware["mode"] == "simulated":
        from nfc_login.hardware.simulated import SimulatedReader
        return SimulatedReader()
    # Imported here: the PN532 library only installs on the Pi.
    from nfc_login.hardware.nfc_reader import PN532Reader
    nfc = config.hardware["nfc"]
    return PN532Reader(poll_timeout=nfc["poll_timeout_seconds"], nfc=nfc,
                       tries=nfc.get("tries", 4))
