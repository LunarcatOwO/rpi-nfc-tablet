# rpi-nfc-tablet: touch NFC sign-in kiosk for a Raspberry Pi 4 B
# Created by LunarcatOwO (https://github.com/LunarcatOwO)
# Copyright (C) 2026 LunarcatOwO
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. It comes WITHOUT ANY WARRANTY; see the LICENSE file.

"""Keeps sign-in times right when the Pi's clock jumps."""

# The Pi has no clock battery. After being off it starts with the time it was
# shut down at, and only gets the real time once it reaches the internet, when
# the clock jumps forward (or back). Someone who signed in before that jump has
# a sign-in time that's wrong by the size of the jump.
#
# The fix: the Pi also has a "monotonic" clock that only counts seconds since
# boot, never jumps, and isn't slowed by heat throttling (it's a hardware timer,
# not counted by the CPU). The wall clock minus the monotonic clock stays the same
# while the time is right. When it changes by more than a couple of minutes, the
# wall clock has been set: every session recorded since this program started is
# moved by the same amount, so durations stay exactly what they really were.
#
# Times are kept in local time (that's what people read), so a daylight saving
# change or a new time zone looks just like a jump too: the clock goes back or
# forward an hour, and sessions open across it are moved the same way, so an
# hour is never gained or lost. Nothing ever waits for the internet: offline,
# the kiosk keeps counting from the last time it knew.

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Callable

from nfc_login.db import Database
from nfc_login.db import repository as repo

log = logging.getLogger(__name__)

JUMP_SECONDS = 120   # smaller corrections (the clock drifting) are left alone
EPOCH = datetime(1970, 1, 1)


def local_seconds() -> float:
    """The local wall clock (as stored in the database) in seconds."""
    time.tzset()   # pick up a new time zone set on the Pi meanwhile
    return (datetime.now() - EPOCH).total_seconds()


class ClockGuard:
    def __init__(self, db: Database, wall: Callable[[], float] | None = None,
                 monotonic: Callable[[], float] = time.monotonic):
        self.db = db
        self.wall = wall or local_seconds
        self.monotonic = monotonic
        self._offset = self.wall() - monotonic()
        self._since = EPOCH + timedelta(seconds=self.wall())   # sessions from here on are ours

    def check(self) -> int:
        """Call often (once a second). Returns the jump fixed, in seconds (0 = none)."""
        offset = self.wall() - self.monotonic()
        jump = offset - self._offset
        if abs(jump) < JUMP_SECONDS:
            return 0
        delta = timedelta(seconds=round(jump))
        with self.db.transaction() as cur:
            moved = repo.shift_sessions(cur, self._since, delta)
        log.warning("clock jumped %+ds: moved %d session(s) recorded since %s",
                    round(jump), moved, self._since)
        self._offset = offset
        self._since += delta
        return round(jump)
