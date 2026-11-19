# rpi-nfc-tablet: touch NFC sign-in kiosk for a Raspberry Pi 4 B
# Created by LunarcatOwO (https://github.com/LunarcatOwO)
# Copyright (C) 2026 LunarcatOwO
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. It comes WITHOUT ANY WARRANTY; see the LICENSE file.

"""Scheduled sign-out: everyone still signed in is signed out at set times each day."""

# Admins set the times in Hours > Scheduled sign-out. Each time either keeps
# people's hours up to that time, or drops them (as if they never signed in).
# The times live in the settings table as "2100k,1530d" (k keep, d drop), and
# when each one last ran as "2100:202611192100,...", so a restart never runs a
# time twice, and a time missed while the Pi was off still runs if it's recent.

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from nfc_login.db import Database
from nfc_login.db import repository as repo
from nfc_login.services.attendance import credited_seconds

TIMES_KEY = "auto_sign_out"
DONE_KEY = "auto_sign_out_done"
MAX_TIMES = 8
CATCH_UP = timedelta(hours=12)   # a time missed by more than this (Pi off) is skipped


class ScheduleError(Exception):
    """A problem to show the admin (bad time, already set...)."""


def parse_time(digits: str) -> str:
    """Typed digits "2100" / "930" -> "21:00" / "09:30"."""
    if not digits.isdigit() or not 3 <= len(digits) <= 4:
        raise ScheduleError("Type the time as 4 digits, e.g. 2100 for 9 PM.")
    hours, minutes = int(digits[:-2]), int(digits[-2:])
    if hours > 23 or minutes > 59:
        raise ScheduleError("That isn't a time: use 0000 to 2359.")
    return f"{hours:02d}:{minutes:02d}"


class AutoSignOut:
    def __init__(self, db: Database, max_session_hours: float = 12,
                 clock: Callable[[], datetime] = datetime.now):
        self.db = db
        self.max_session = timedelta(hours=max_session_hours)
        self.clock = clock

    # ------------------------------------------------------------ the times

    def times(self) -> list[tuple[str, bool]]:
        """[("21:00", keep_hours)], earliest first."""
        with self.db.transaction() as cur:
            raw = repo.get_setting(cur, TIMES_KEY) or ""
        out = []
        for part in filter(None, raw.split(",")):
            out.append((f"{part[:2]}:{part[2:4]}", part[4:] != "d"))
        return sorted(out)

    def _save(self, cur, times: list[tuple[str, bool]]) -> None:
        repo.set_setting(cur, TIMES_KEY, ",".join(
            t.replace(":", "") + ("k" if keep else "d") for t, keep in sorted(times)))

    def add(self, time: str, keep: bool) -> None:
        times = self.times()
        if any(t == time for t, _ in times):
            raise ScheduleError(f"{time} is already on the list.")
        if len(times) >= MAX_TIMES:
            raise ScheduleError(f"Up to {MAX_TIMES} times. Remove one first.")
        with self.db.transaction() as cur:
            self._save(cur, times + [(time, keep)])
            # Counts as done for today if it has already passed: it starts tomorrow.
            done = self._done(cur)
            done[time] = self._last_due(time, self.clock())
            self._save_done(cur, done)

    def remove(self, time: str) -> None:
        with self.db.transaction() as cur:
            self._save(cur, [(t, k) for t, k in self.times() if t != time])

    def set_keep(self, time: str, keep: bool) -> None:
        with self.db.transaction() as cur:
            self._save(cur, [(t, keep if t == time else k) for t, k in self.times()])

    # ------------------------------------------------------------ running them

    @staticmethod
    def _last_due(time: str, now: datetime) -> datetime:
        """The most recent moment this time came round (today's, or yesterday's)."""
        hours, minutes = int(time[:2]), int(time[3:])
        due = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        return due if due <= now else due - timedelta(days=1)

    def _done(self, cur) -> dict[str, datetime]:
        raw = repo.get_setting(cur, DONE_KEY) or ""
        done = {}
        for part in filter(None, raw.split(",")):
            time, _, stamp = part.partition(":")
            try:
                done[f"{time[:2]}:{time[2:]}"] = datetime.strptime(stamp, "%Y%m%d%H%M")
            except ValueError:
                pass
        return done

    def _save_done(self, cur, done: dict[str, datetime]) -> None:
        repo.set_setting(cur, DONE_KEY, ",".join(
            f"{t.replace(':', '')}:{d:%Y%m%d%H%M}" for t, d in sorted(done.items())))

    def run_due(self) -> list[tuple[str, bool, int]]:
        """Run any time that has come round: [(time, keep_hours, people signed out)]."""
        times = self.times()
        if not times:
            return []
        now = self.clock()
        ran = []
        with self.db.transaction() as cur:
            done = self._done(cur)
            for time, keep in times:
                due = self._last_due(time, now)
                if time in done and done[time] >= due:
                    continue
                done[time] = due
                if now - due > CATCH_UP:
                    continue   # long ago (the Pi was off): don't sign anyone out late
                count = 0
                for session in repo.list_open_sessions(cur):
                    if session["sign_in_at"] >= due:
                        continue   # signed in after the time (a late catch-up run)
                    # Signed out at the scheduled time itself, even when run late.
                    credit = (credited_seconds(session["sign_in_at"], due, self.max_session)
                              if keep else 0)
                    repo.close_session(cur, session["id"], due,
                                       "scheduled" if keep else "scheduled_drop", credit)
                    count += 1
                ran.append((time, keep, count))
            self._save_done(cur, done)
        return ran
