# rpi-nfc-tablet: touch NFC sign-in kiosk for a Raspberry Pi 4 B
# Created by LunarcatOwO (https://github.com/LunarcatOwO)
# Copyright (C) 2026 LunarcatOwO
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. It comes WITHOUT ANY WARRANTY; see the LICENSE file.

"""Sign-in blocks: times when nobody can sign in (signing out always works)."""

# Admins set them in Hours > Sign-in blocks: a start, an end and the days of the
# week, e.g. 09:00-15:00 Mon-Fri (class time). A block that ends before it starts
# runs overnight (22:00-06:00), and its day is the day it starts. Admins can still
# sign people in from Hours > Sign someone in or out. Stored in the settings table
# as "0900-1500:12345,..." (1 = Monday ... 7 = Sunday).

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from nfc_login.db import Database
from nfc_login.db import repository as repo

KEY = "sign_in_blocks"
MAX_BLOCKS = 8
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass(frozen=True)
class Block:
    start: str          # "09:00"
    end: str            # "15:00"
    days: str           # "12345": the weekdays it starts on (1 = Monday)

    @property
    def days_text(self) -> str:
        return days_text(self.days)

    def covers(self, now: datetime) -> bool:
        """True if signing in is blocked at ``now``."""
        start = now.replace(hour=int(self.start[:2]), minute=int(self.start[3:]),
                            second=0, microsecond=0)
        end = start.replace(hour=int(self.end[:2]), minute=int(self.end[3:]))
        if end <= start:
            end += timedelta(days=1)   # overnight
        for began in (start, start - timedelta(days=1)):   # today's, or last night's
            if str(began.isoweekday()) in self.days and began <= now < end - (start - began):
                return True
        return False


def days_text(days: str) -> str:
    """"12345" -> "Mon-Fri", "67" -> "Sat, Sun", "1234567" -> "Every day"."""
    days = "".join(sorted(set(days)))
    named = {"1234567": "Every day", "12345": "Mon-Fri", "67": "Weekends"}
    if days in named:
        return named[days]
    return ", ".join(DAY_NAMES[int(d) - 1] for d in days) or "No days"


class SignInBlocks:
    def __init__(self, db: Database):
        self.db = db

    def list(self) -> list[Block]:
        with self.db.transaction() as cur:
            raw = repo.get_setting(cur, KEY) or ""
        blocks = []
        for part in filter(None, raw.split(",")):
            times, _, days = part.partition(":")
            start, _, end = times.partition("-")
            blocks.append(Block(f"{start[:2]}:{start[2:]}", f"{end[:2]}:{end[2:]}", days))
        return sorted(blocks, key=lambda b: (b.start, b.end, b.days))

    def _save(self, blocks: list[Block]) -> None:
        with self.db.transaction() as cur:
            repo.set_setting(cur, KEY, ",".join(
                f"{b.start.replace(':', '')}-{b.end.replace(':', '')}:{b.days}" for b in blocks))

    def add(self, block: Block) -> None:
        blocks = self.list()
        if len(blocks) >= MAX_BLOCKS:
            raise ValueError(f"Up to {MAX_BLOCKS} blocks. Remove one first.")
        self._save(blocks + [block])

    def replace(self, old: Block, new: Block) -> None:
        self._save([new if b == old else b for b in self.list()])

    def remove(self, block: Block) -> None:
        self._save([b for b in self.list() if b != block])

    def blocking(self, now: datetime) -> Block | None:
        """The block stopping sign-ins at ``now``, if any."""
        return next((b for b in self.list() if b.covers(now)), None)
