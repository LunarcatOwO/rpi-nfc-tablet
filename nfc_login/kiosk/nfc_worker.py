# rpi-nfc-tablet: touch NFC sign-in kiosk for a Raspberry Pi 4 B
# Created by LunarcatOwO (https://github.com/LunarcatOwO)
# Copyright (C) 2026 LunarcatOwO
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. It comes WITHOUT ANY WARRANTY; see the LICENSE file.

"""Background thread that polls the NFC reader and hands scans to the controller."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from nfc_login.kiosk.controller import KioskController, Screen

log = logging.getLogger(__name__)


class NfcWorker(threading.Thread):
    """Asks the reader for a card over and over; each new card goes to the controller."""

    def __init__(self, reader, controller: KioskController, publish: Callable[[Screen], None]):
        super().__init__(daemon=True, name="nfc")
        self.reader = reader
        self.controller = controller
        self.publish = publish
        self._stopped = threading.Event()
        # How the reader is doing, for System info. Only this thread talks to the
        # reader; the screen reads these instead of asking the PN532 itself.
        self.last_ok: float | None = None     # monotonic time of the last good poll
        self.last_card: float | None = None   # ...and of the last card seen
        self.failures = 0                     # failed polls in a row

    def status(self, now: float | None = None) -> str:
        """"Working · last card 2m ago", "Not answering (3 failed reads)"..."""
        now = time.monotonic() if now is None else now
        if self.failures:
            return f"Not answering ({self.failures} failed read{'s' if self.failures != 1 else ''})"
        if self.last_ok is None:
            return "Starting"
        if now - self.last_ok > 10:
            return "Stuck (no answer for {:.0f} s)".format(now - self.last_ok)
        if self.last_card is None:
            return "Working · no card yet"
        ago = int(now - self.last_card)
        when = (f"{ago}s" if ago < 60 else f"{ago // 60}m" if ago < 3600
                else f"{ago // 3600}h {ago % 3600 // 60}m")
        return f"Working · last card {when} ago"

    def run(self) -> None:
        present: str | None = None  # the card on the reader now, if any
        while not self._stopped.is_set():
            try:
                uid = self.reader.read_uid()  # waits up to poll_timeout for a card
            except Exception:  # I2C hiccups shouldn't kill the kiosk
                log.exception("NFC read failed")
                self.failures += 1
                time.sleep(1)
                continue
            self.last_ok = time.monotonic()
            self.failures = 0
            if uid is None:
                present = None  # card taken away: the next tap counts again
                continue
            if uid == present:
                continue  # same card still sitting on the reader
            present = uid
            self.last_card = self.last_ok
            try:
                screen = self.controller.handle_card(uid)
            except Exception as exc:
                log.exception("scan of %s failed", uid)
                screen = Screen("Something went wrong", [str(exc)], "error", hold_seconds=6)
            self.publish(screen)  # hand the result to the screen's thread

    def stop(self) -> None:
        self._stopped.set()
