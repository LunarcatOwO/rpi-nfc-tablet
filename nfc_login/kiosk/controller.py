# rpi-nfc-tablet: touch NFC sign-in kiosk for a Raspberry Pi 4 B
# Created by LunarcatOwO (https://github.com/LunarcatOwO)
# Copyright (C) 2026 LunarcatOwO
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. It comes WITHOUT ANY WARRANTY; see the LICENSE file.

"""Kiosk behaviour: what happens on a card scan or a tap."""

# Each handler returns a Screen saying what to show, and knows nothing about
# the web page, so all of this can be tested without hardware.
#
# Everything is touch. Each button on the screen sends one "key":
#   A-D  a team button on the ID screen (B then 0 0 7 = B007)
#   0-9  the number pad, or the Nth item of a menu or list
#   #    the main action (Enter, Sign in, Yes...)
#   *    delete one character; from the start screen, the admin gear (asks for the PIN)
#   X    the back arrow (or a swipe in from the left): back one step, whatever is typed
#   ?    "Search by name" on the admin ID screens and the leaderboard
#   ~    the start screen's Leaderboard button
# Admin menu: 1 People, 2 Hours, 3 System (see the *_MENU_LINES below); the side
# list in those menus jumps between them with handle_nav().
# Names, name searches and Wi-Fi passwords come from the on-screen keyboard via
# handle_char(). A card tapped while someone is in admin or the leaderboard shows a
# small pop-up (Screen.toast) and leaves that screen as it was.

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from nfc_login.hardware.nfc_reader import TagWriteError
from nfc_login.services import ids, timefmt
from nfc_login.services.attendance import (
    IGNORED,
    SIGNED_IN,
    SIGNED_OUT,
    AttendanceError,
    AttendanceService,
    SignInClosed,
    UserStats,
)
from nfc_login.services.seasons import SeasonError
from nfc_login.services.system import (EAP_METHODS, EnterpriseLogin, SystemActionError,
                                       WifiOffError)
from nfc_login.services.blocks import MAX_BLOCKS, Block, days_text
from nfc_login.services.schedule import ScheduleError, parse_time
from nfc_login.services.updates import UpdateError
from nfc_login.services.users import UserError, UserService
from nfc_login.tags.payload import TagTooSmall, build_message

log = logging.getLogger(__name__)

# The states (which screen is up). self.state holds one of these.
IDLE = "idle"
USER_ID = "user_id"           # typing an ID from the idle screen
USER_MENU = "user_menu"       # confirm screen after typing an ID: # signs in/out
ADMIN_PIN = "admin_pin"
BUSY = "busy"                 # a background job (Wi-Fi, update, power) is running
BOARD = "board"               # the whole leaderboard (context["query"] filters it)
BOARD_SEARCH = "board_search"  # typing a name to find someone's rank

ADMIN_MENU = "admin_menu"
PEOPLE_MENU = "people_menu"
HOURS_MENU = "hours_menu"
HERE_LIST = "here_list"       # admin: who is here, a page at a time
OFF_LIST = "off_list"         # admin: who is deactivated, a page at a time
SYSTEM_MENU = "system_menu"
MENUS = (ADMIN_MENU, PEOPLE_MENU, HOURS_MENU, SYSTEM_MENU)

# People
NEW_TEAM = "new_team"         # adding a user: which team
NEW_NAME = "new_name"         # adding a user: their name, on the screen keyboard
ENROLL_ID = "enroll_id"
ENROLL_SCAN = "enroll_scan"
UNCARD_ID = "uncard_id"       # whose cards to remove
MOVE_ID = "move_id"           # whose team to change
PICK_TEAM = "pick_team"       # a team is chosen (legacy card's owner, or admin via MOVE_ID)
RENAME_ID = "rename_id"
RENAME_NAME = "rename_name"
ACTIVE_ID = "active_id"       # whom to deactivate / reactivate
NAME_SEARCH = "name_search"   # admin: find someone by name, for context["for"]
PERSON = "person"             # admin: one person's page, with everything to change
# Hours
ADJUST_ID = "adjust_id"
ADJUST_AMOUNT = "adjust_amount"
TOGGLE_ID = "toggle_id"       # sign someone in or out without a PIN
SEASON_PIN = "season_pin"     # admin PIN again before starting a new season
PAST_LIST = "past_list"       # the finished seasons, to pick one
PAST_SEASON = "past_season"   # one finished season's hours, a page at a time
SCHEDULE = "schedule"         # the scheduled sign-out times
SCHEDULE_TIME = "schedule_time"  # adding one: the time, on the number pad
SCHEDULE_KEEP = "schedule_keep"  # adding one: keep or drop the hours?
SCHEDULE_ITEM = "schedule_item"  # one time: keep/drop its hours, or remove it
BLOCKS = "blocks"             # sign-in blocks: times nobody can sign in
BLOCK_START = "block_start"   # adding one: from (number pad)
BLOCK_END = "block_end"       # adding one: until (number pad)
BLOCK_DAYS = "block_days"     # adding or changing one: which days
BLOCK_ITEM = "block_item"     # one block: change its days, or remove it
# System
UPDATE_CONFIRM = "update_confirm"
WIFI_LIST = "wifi_list"
WIFI_PASSWORD = "wifi_password"
WIFI_LOGIN = "wifi_login"     # an enterprise network: username, password and options
WIFI_FIELD = "wifi_field"     # typing one of those (context["field"])
ADMINPIN_OLD = "adminpin_old"  # current admin PIN before changing it
ADMINPIN_NEW = "adminpin_new"
ADMINPIN_AGAIN = "adminpin_again"
CONFIRM = "confirm"           # "are you sure?" for the action in context["action"]

ID_STATES = (USER_ID, ENROLL_ID, ADJUST_ID, MOVE_ID, UNCARD_ID, RENAME_ID, ACTIVE_ID,
             TOGGLE_ID)
PIN_STATES = (ADMIN_PIN, ADMINPIN_NEW, ADMINPIN_AGAIN, SEASON_PIN, ADMINPIN_OLD)
NAME_STATES = (NEW_NAME, RENAME_NAME)
TEXT_STATES = (*NAME_STATES, WIFI_PASSWORD, WIFI_FIELD, NAME_SEARCH, BOARD_SEARCH)
# A card tapped on these screens takes over the whole screen; anywhere else
# (admin, the leaderboard) it shows a pop-up and the screen stays as it was.
FULL_SCREEN_SCANS = (IDLE, USER_ID, USER_MENU, ENROLL_SCAN)
# The steps an action on someone's page can lead to: back from them returns there.
AFTER_PERSON = (PICK_TEAM, RENAME_NAME, ADJUST_AMOUNT, ENROLL_SCAN, CONFIRM)

# Where * goes from an empty entry.
BACK = {
    USER_MENU: USER_ID,       # "Not me": type the ID again
    PEOPLE_MENU: ADMIN_MENU, HOURS_MENU: ADMIN_MENU, SYSTEM_MENU: ADMIN_MENU,
    NEW_TEAM: PEOPLE_MENU, ENROLL_ID: PEOPLE_MENU, UNCARD_ID: PEOPLE_MENU,
    MOVE_ID: PEOPLE_MENU, RENAME_ID: PEOPLE_MENU,
    ACTIVE_ID: PEOPLE_MENU, OFF_LIST: PEOPLE_MENU, NEW_NAME: NEW_TEAM, RENAME_NAME: RENAME_ID,
    ADJUST_ID: HOURS_MENU, TOGGLE_ID: HOURS_MENU, SEASON_PIN: HOURS_MENU, HERE_LIST: HOURS_MENU,
    PAST_LIST: HOURS_MENU, PAST_SEASON: PAST_LIST,
    SCHEDULE: HOURS_MENU, SCHEDULE_TIME: SCHEDULE, SCHEDULE_KEEP: SCHEDULE_TIME,
    SCHEDULE_ITEM: SCHEDULE,
    BLOCKS: HOURS_MENU, BLOCK_START: BLOCKS, BLOCK_END: BLOCK_START, BLOCK_ITEM: BLOCKS,
    ADJUST_AMOUNT: ADJUST_ID,
    UPDATE_CONFIRM: SYSTEM_MENU, WIFI_LIST: SYSTEM_MENU, ADMINPIN_NEW: SYSTEM_MENU,
    ADMINPIN_OLD: SYSTEM_MENU,
    WIFI_PASSWORD: WIFI_LIST, ADMINPIN_AGAIN: ADMINPIN_NEW,
    WIFI_LOGIN: WIFI_LIST, WIFI_FIELD: WIFI_LOGIN,
}

# How kiosk.html lays out each state's screen (the rest are "page").
VIEWS = {
    ADMIN_MENU: "admin", PEOPLE_MENU: "menu", HOURS_MENU: "menu", SYSTEM_MENU: "menu",
    USER_MENU: "dialog", CONFIRM: "dialog", UPDATE_CONFIRM: "dialog",
    SCHEDULE_KEEP: "dialog", SCHEDULE_ITEM: "dialog", BLOCK_ITEM: "dialog",
    PICK_TEAM: "teams", NEW_TEAM: "teams", ENROLL_SCAN: "scan", BOARD: "board",
}
NAV = {PEOPLE_MENU: "people", HOURS_MENU: "hours", SYSTEM_MENU: "system"}
CONFIRM_VERBS = {"restart_app": "Restart", "close_app": "Close it", "reboot": "Restart",
                 "poweroff": "Shut down"}

# Limits and timings.
MAX_PIN = 8
MAX_AMOUNT = 4                # HHMM, up to 99h 59m per adjustment
MAX_NAME = 40
SETUP_REMINDER = "Setup changed in an update: run bash scripts/setup-pi.sh"
MAX_PASSWORD = 63             # longest WPA passphrase
MAX_LOGIN = 128               # enterprise usernames and passwords can be longer
# The enterprise sign-in's typed fields: key -> (EnterpriseLogin attribute, title).
WIFI_FIELDS = {"username": ("username", "Username"),
               "password": ("password", "Password"),
               "anon": ("anonymous_identity", "Anonymous identity"),
               "domain": ("domain", "Server domain")}
MAX_WIFI_LIST = 8
HERE_PAGE = 8                 # people per page on the admin lists
SEARCH_SHOWN = 4              # name search results that fit above the keyboard
DAY_NAMES_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MAX_PIN_FAILURES = 5
LOCKOUT_SECONDS = 60
POWER_DELAY_SECONDS = 2       # time to read the screen before the Pi goes down
WIFI_ON_SECONDS = 5           # after switching the radio on, before scanning again

PEOPLE_MENU_LINES = [
    "1  Add a person",
    "2  Enroll a card",
    "3  Remove someone's cards",
    "4  Change someone's team",
    "5  Rename someone",
    "6  Deactivate / reactivate",
    "7  Deactivated people",
    "8  Find someone by name",
]
HOURS_MENU_LINES = [
    "1  Add / subtract hours",
    "2  Sign someone in or out",
    "3  Who is here",
    "4  Sign everyone out",
    "5  Start a new season",
    "6  Past seasons",
    "7  Scheduled sign-out",
    "8  Sign-in blocks",
]


@dataclass
class Screen:
    """What the display shows.

    A line made only of "K  label" parts (K a key, two spaces, then the label;
    parts separated by three or more spaces, e.g. "#  yes      *  back") is drawn
    as buttons that send that key when tapped. Other lines are plain text: don't
    put two spaces after a lone key character in them.
    """

    title: str
    lines: list[str] = field(default_factory=list)
    tone: str = "info"                  # info | success | warning | error | prompt
    entry: str | None = None            # what's been typed (already masked for PINs)
    hold_seconds: float | None = None   # return to idle after this long
    refresh_leaderboard: bool = False   # also refreshes the "here now" list
    sound: str | None = None            # sound to play; by default from the tone
    # on-screen keys: "name" / "text" (letters, for names / passwords),
    # "id" (team buttons + number pad), "numpad" (number pad: PINs, amounts)
    keyboard: str | None = None
    # How the page lays it out: "idle" (tap your card), "scan" (waiting for a card),
    # "result" (big icon, goes back by itself), "dialog" (a box over the last
    # screen), "admin" (the three tiles), "menu" (a menu with the side list),
    # "teams" (big team cards), or "page" (title, text and buttons).
    view: str = "page"
    nav: str | None = None              # "menu": which side list item is lit
    stats: list = field(default_factory=list)   # [(label, value)] shown as cards
    person: dict | None = None          # {"name", "sub", "team"}: the avatar row
    close_app: bool = False             # the window should close itself
    minimize_app: bool = False          # the window should minimize (kiosk keeps running)
    restart_app: bool = False           # the app should restart itself (e.g. after an update)
    busy: bool = False                  # waiting on a background job: show a spinner
    # People as rows (leaderboard, name search): [{"name", "sub", "team", "right",
    # "rank", "key"}]; a row with a "key" is a button sending it.
    rows: list = field(default_factory=list)
    toast: bool = False                 # a pop-up over whatever is showing (card tapped mid-menu)

    @property
    def chime(self) -> str | None:
        """Sound to play when this screen appears (kiosk.html's SOUNDS)."""
        return self.sound or {"success": "success", "warning": "warning",
                              "error": "error"}.get(self.tone)


def parse_amount(digits: str) -> int:
    """Typed amount as H..HMM -> seconds. '130' = 1h 30m, '45' = 45m, '200' = 2h."""
    if not digits:
        return 0
    minutes = int(digits[-2:])
    hours = int(digits[:-2] or 0)
    if minutes >= 60:
        raise ValueError("Minutes must be 00-59 (type 130 for 1h 30m).")
    return (hours * 60 + minutes) * 60


def _start_thread(fn: Callable[[], None]) -> None:
    """Run a slow job (Wi-Fi, update, power) in the background."""
    threading.Thread(target=fn, daemon=True, name="kiosk-job").start()


class KioskController:
    """The kiosk's state machine.

    ``self.state`` says which screen is up (IDLE, ADMIN_PIN, PEOPLE_MENU, ...),
    ``self.buffer`` holds what has been typed on it so far, and ``self.context``
    carries data between steps (e.g. the user being edited). Each card scan or
    key press moves the state on and returns the next Screen to draw.
    """

    def __init__(
        self,
        attendance: AttendanceService,
        users: UserService,
        reader=None,
        site_url: str = "",
        write_tags: bool = True,
        result_seconds: float = 6,
        idle_timeout: float = 30,
        monotonic: Callable[[], float] = time.monotonic,
        seasons=None,
        system=None,
        updates=None,
        spawn: Callable[[Callable[[], None]], None] = _start_thread,
        sleep: Callable[[float], None] = time.sleep,
        reopen_hint: str = "To start it again, restart the Pi.",
        schedule=None,
        clock_guard=None,
    ):
        self.attendance = attendance
        self.users = users
        self.reader = reader
        self.site_url = site_url
        self.write_tags = write_tags
        self.result_seconds = result_seconds
        self.idle_timeout = idle_timeout
        self.monotonic = monotonic
        self.seasons = seasons      # SeasonService, for "Start a new season"
        self.system = system        # SystemActions: info, Wi-Fi, power
        self.updates = updates      # UpdateChecker
        self.schedule = schedule    # AutoSignOut: Hours > Scheduled sign-out
        self.clock_guard = clock_guard   # ClockGuard: fixes times when the clock jumps
        self._spawn = spawn
        self._sleep = sleep
        self.reopen_hint = reopen_hint   # shown when the app is closed from the menu
        # The window sets this so background jobs can show their result.
        self.publish: Callable[[Screen], None] | None = None
        # team button key -> section letter, normally {"A": "A", "B": "B", ...}
        self.section_keys = {s["key"]: s["letter"] for s in users.sections if s["key"]}
        self.mentors = ids.MENTORS in users.section_names
        # PICK_TEAM choices: digit -> section letter, in config order
        self.team_choices = {str(n): s["letter"]
                             for n, s in enumerate(users.sections[:9], start=1)}

        self._lock = threading.RLock()
        self.state = IDLE
        self.buffer = ""
        self.context: dict = {}
        self._job: object | None = None
        self._last_input = monotonic()
        self._pin_failures = 0
        self._locked_until = 0.0

    # ------------------------------------------------------------ screens

    def idle_screen(self) -> Screen:
        """The start screen: tap a card, or type an ID."""
        lines = ["Hold it on the reader to sign in or out.",
                 "#  No card? Type my ID      ~  Leaderboard"]
        if self._installing():
            lines = ["Installing an update: cards still work.", *lines]
        block = self._sign_in_block()
        if block:
            lines[-2] = f"Signing in is closed until {block.end}. Signing out still works."
        return Screen("Tap your card", lines, tone="info", view="idle")

    def _sign_in_block(self):
        """The sign-in block in force now, if any (Hours > Sign-in blocks)."""
        blocks = getattr(self.attendance, "sign_in_blocks", None)
        if blocks is None:
            return None
        try:
            return blocks.blocking(self.attendance.clock())
        except Exception:   # a database hiccup shouldn't stop the start screen
            return None

    def _here_count(self) -> str:
        """"6 here now. " for the admin Hours tile ("" if it can't be counted)."""
        try:
            return f"{len(self.attendance.currently_signed_in())} here now. "
        except Exception:   # a database hiccup shouldn't stop the admin menu
            return ""

    def _installing(self) -> bool:
        return self.updates is not None and getattr(self.updates, "installing", False)

    def _result(self, title, lines, tone, refresh=False) -> Screen:
        """A result screen that goes back to idle after a few seconds."""
        return Screen(title, lines, tone, hold_seconds=self.result_seconds,
                      refresh_leaderboard=refresh, view="result")

    def _reset(self) -> None:
        """Back to idle, forgetting anything typed."""
        self.state = IDLE
        self.buffer = ""
        self.context = {}

    def _go(self, state: str, context: dict | None = None) -> Screen:
        """Move to another state with an empty entry, and draw it."""
        self.state, self.buffer = state, ""
        if context is not None:
            self.context = context
        return self._redraw()

    # ------------------------------------------------------------ cards

    def handle_card(self, uid: str) -> Screen:
        """A card was tapped (called from the NFC thread)."""
        with self._lock:  # never at the same time as a key press
            if self.state not in FULL_SCREEN_SCANS and not self.context.get("legacy"):
                # Not counted as a touch: cards tapped by passers-by mustn't keep an
                # admin menu open after the admin walks away.
                return self._toast_scan(uid)
            self._last_input = self.monotonic()
            if self.state == ENROLL_SCAN:  # an admin is enrolling this card
                return self._enroll(uid)
            # Signing in always works, even mid-menu or while a background job
            # runs (its result is then dropped, unless it must be shown).
            self._reset()
            try:
                user = self.attendance.user_for_tag(uid)
            except AttendanceError:
                user = None  # scan_tag below reports why
            if user and user["section"] == ids.UNSORTED:  # imported card, no team yet
                return self._legacy_card(uid, user)
            try:
                result = self.attendance.scan_tag(uid)
            except SignInClosed as exc:
                return self._result("Signing in is closed", [str(exc)], "warning")
            except AttendanceError as exc:
                return self._result("Not signed in", [str(exc), f"Card {uid}"], "error")
            screen = self._scan_screen(result.action, result.stats, result.session_seconds,
                                       result.notes)
            if result.action != IGNORED:
                # The card is still on the reader: write the new stats onto it.
                screen.lines.extend(self._write_card(result.stats))
            return screen

    def _toast_scan(self, uid: str) -> Screen:
        """A card tapped mid-menu: sign them in or out, and say so in a pop-up."""
        try:
            user = self.attendance.user_for_tag(uid)
        except AttendanceError:
            user = None
        if user and user["section"] == ids.UNSORTED:
            # Picking a team needs the whole screen, so that waits for the start screen.
            return Screen(f"{user['username']}: not signed in",
                          ["Pick your team first: tap again on the start screen."],
                          "warning", toast=True, sound="attention")
        try:
            result = self.attendance.scan_tag(uid)
        except SignInClosed as exc:
            return Screen("Signing in is closed", [str(exc)], "warning", toast=True)
        except AttendanceError as exc:
            return Screen("Not signed in", [str(exc)], "error", toast=True)
        stats = result.stats
        team = self.users.team_name(stats.section, short=True)
        if result.action == SIGNED_IN:
            title, sound = f"{stats.username} signed in", "sign_in"
        elif result.action == SIGNED_OUT:
            title, sound = f"{stats.username} signed out", "sign_out"
        else:
            title, sound = stats.username, "ignored"
        lines = [*result.notes, f"{stats.code}  ·  {team}  ·  {stats.total_text}"]
        if result.action != IGNORED:
            lines += self._write_card(stats)
        return Screen(title, lines, "success" if result.action != IGNORED else "warning",
                      toast=True, sound=sound, person={"name": stats.username, "sub": "",
                                                       "team": stats.section},
                      refresh_leaderboard=result.action != IGNORED)

    def _legacy_card(self, uid: str, user: dict) -> Screen:
        """An imported card with no team yet: its owner picks one first."""
        self.state = PICK_TEAM
        self.context = {"uid": uid, "user": user, "legacy": True}
        screen = self._redraw()
        screen.sound = "attention"
        return screen

    def _scan_screen(self, action, stats: UserStats, session_seconds, notes) -> Screen:
        """The Welcome / Goodbye screen after a scan."""
        who = f"{stats.code}  ·  {self.users.team_name(stats.section)}"
        if action == SIGNED_IN:
            title, tone, sound = f"Welcome, {stats.username}!", "success", "sign_in"
            lines = [f"Signed in at {timefmt.format_timestamp(stats.last_sign_in)}  ·  {who}"]
        elif action == SIGNED_OUT:
            title, tone, sound = f"Goodbye, {stats.username}!", "success", "sign_out"
            lines = [f"This session: {timefmt.format_duration(session_seconds)}  ·  {who}"]
        else:
            title, tone, sound = stats.username, "warning", "ignored"
            lines = [who]
        screen = self._result(title, notes + lines, tone, refresh=action != IGNORED)
        screen.stats = _stats(stats)
        screen.sound = sound
        return screen

    def _write_card(self, stats: UserStats) -> list[str]:
        """Write the latest stats onto the card still on the reader."""
        if not (self.write_tags and self.reader):
            return []
        try:
            capacity = self.reader.ndef_capacity()
            if capacity is None:
                return ["(This card type can't store info; sign-in still counted.)"]
            self.reader.write_ndef(build_message(stats, self.site_url, capacity))
        except (TagWriteError, TagTooSmall) as exc:
            return [f"Card info not updated: {exc}"]
        return []

    def _enroll(self, uid: str) -> Screen:
        """Link the tapped card to the user chosen in People > 2."""
        user = self.context["user"]
        self._reset()
        try:
            self.users.enroll_tag(uid, user["id"])
        except UserError as exc:
            return self._result("Card not enrolled", [str(exc)], "error")
        stats = self.attendance.user_stats(user["id"])
        lines = [f"Card {uid} now belongs to {user['username']} ({user['code']})."]
        lines += self._write_card(stats)
        return self._result("Card enrolled", lines, "success")

    # ------------------------------------------------------------ keys

    def handle_key(self, key: str) -> Screen | None:
        """A button was tapped (see the key list at the top); returns the next screen (None = no change)."""
        with self._lock:
            self._last_input = self.monotonic()
            if self.state == BUSY:
                return None   # wait for the background job
            if self._locked_until > self.monotonic():
                return self._result("Locked for a minute", ["Too many wrong PINs. Try again soon."],
                                    "error")
            if key == "X":   # the back arrow: one step back, dropping anything typed
                if self.state == IDLE:
                    return None
                self.buffer = ""
                return self._star()
            if key == "*":
                return self._star()
            # Most screens have their own key handler...
            handler = {
                IDLE: self._idle_key,
                USER_MENU: self._user_menu_key,
                ADMIN_MENU: self._admin_choice,
                PEOPLE_MENU: self._people_choice,
                HOURS_MENU: self._hours_choice,
                SYSTEM_MENU: self._system_choice,
                ADJUST_AMOUNT: self._amount_key,
                PICK_TEAM: self._pick_team_key,
                NEW_TEAM: self._new_team_key,
                CONFIRM: self._confirm_key,
                UPDATE_CONFIRM: self._update_confirm_key,
                WIFI_LIST: self._wifi_list_key,
                WIFI_LOGIN: self._wifi_login_key,
                HERE_LIST: self._here_list_key,
                OFF_LIST: self._here_list_key,   # same paging
                PAST_SEASON: self._here_list_key,
                PAST_LIST: self._past_list_key,
                PERSON: self._person_key,
                SCHEDULE: self._schedule_key,
                SCHEDULE_TIME: self._schedule_time_key,
                SCHEDULE_KEEP: self._schedule_keep_key,
                SCHEDULE_ITEM: self._schedule_item_key,
                BLOCKS: self._blocks_key,
                BLOCK_START: self._block_time_key,
                BLOCK_END: self._block_time_key,
                BLOCK_DAYS: self._block_days_key,
                BLOCK_ITEM: self._block_item_key,
                BOARD: self._board_key,
            }.get(self.state)
            if handler:
                return handler(key)
            # ...the rest share one per kind of entry.
            if self.state in ID_STATES:
                return self._id_key(key)
            if self.state in PIN_STATES:
                return self._pin_key(key)
            if self.state in TEXT_STATES:
                return self._text_key(key)
            return None  # ENROLL_SCAN waits for a card

    def handle_char(self, char: str) -> Screen | None:
        """A key from the on-screen (or USB) keyboard: a character, "\\b" delete or "\\n" Done."""
        with self._lock:
            if self.state not in TEXT_STATES:
                return None
            self._last_input = self.monotonic()
            if char == "\n":
                return self._text_key("#")
            if char == "\b":
                self.buffer = self.buffer[:-1]
                return self._redraw()
            if len(char) != 1 or not char.isprintable():
                return self._redraw()
            if self.state in (WIFI_PASSWORD, WIFI_FIELD):
                if len(self.buffer) < (MAX_PASSWORD if self.state == WIFI_PASSWORD
                                       else MAX_LOGIN):
                    self.buffer += char
                return self._redraw()
            self.buffer = _add_name_char(self.buffer, char)
            return self._redraw()

    def handle_nav(self, menu: str) -> Screen | None:
        """The side list in the admin menus: jump to People, Hours or System."""
        target = {"people": PEOPLE_MENU, "hours": HOURS_MENU, "system": SYSTEM_MENU}.get(menu)
        with self._lock:
            # Only from a menu: the admin PIN has been typed, and nothing is half done.
            if target is None or self.state not in MENUS:
                return None
            self._last_input = self.monotonic()
            return self._go(target, {})

    def background_checks(self) -> Screen | None:
        """Called every few seconds: fix times after a clock jump, then run any
        scheduled sign-out (a pop-up says so)."""
        with self._lock:
            if self.clock_guard is not None:
                try:
                    self.clock_guard.check()
                except Exception:   # the database being down mustn't stop the kiosk
                    log.exception("clock check failed")
            if self.schedule is None:
                return None
            try:
                ran = self.schedule.run_due()
            except Exception:
                log.exception("scheduled sign-out failed")
                return None
            people = sum(count for _, _, count in ran)
            if not ran or not people:
                return None
            kept = all(keep for _, keep, count in ran if count)
            return Screen(f"Scheduled sign-out: {people} signed out", [
                "Their hours up to then count." if kept else "Their hours this time weren't counted.",
            ], "info", toast=True, sound="sign_out", refresh_leaderboard=True)

    def check_timeout(self) -> Screen | None:
        """Go back to the start screen when nobody has touched it for a while."""
        with self._lock:
            # The leaderboard gets longer: scrolling it doesn't count as a touch.
            limit = self.idle_timeout * (3 if self.state == BOARD else 1)
            if (self.state not in (IDLE, BUSY)
                    and self.monotonic() - self._last_input > limit):
                self._reset()
                return self.idle_screen()
            return None

    def _star(self) -> Screen:
        """* key: opens the admin menu from idle, otherwise delete or back (also X, emptied)."""
        if self.state == IDLE:
            if not self.users.admin_pin_set():
                return self._result("No admin PIN",
                                    ["Set one with: python3 -m nfc_login.admin set-admin-pin"],
                                    "warning")
            self.state = ADMIN_PIN
            return self._redraw()
        # Something typed: delete one character (but keep a lone team letter).
        if self.buffer and not (self.state in ID_STATES and self.buffer.isalpha()):
            self.buffer = self.buffer[:-1]
            return self._redraw()
        # Empty entry (or only the team letter): go back a step.
        self.buffer = ""
        if self.state in MENUS and self.context.pop("viewing", False):
            return self._redraw()
        if self.context.get("from_person") and self.state in AFTER_PERSON:
            return self._open_person(self.context["user"]["id"])
        if self.state == NAME_SEARCH:   # back to the ID screen (or People)
            back = self.context["for"]
            return self._go(PEOPLE_MENU if back == PERSON else back, {})
        if self.state == PERSON:        # back to the search, as it was
            query = self.context.get("query", "")
            self._go(NAME_SEARCH, {"for": PERSON})
            self.buffer = query
            return self._redraw()
        if self.state == BOARD_SEARCH:
            return self._go(BOARD, {})
        if self.state == PICK_TEAM and "uid" not in self.context:
            return self._go(MOVE_ID)
        if self.state == ADMIN_PIN and self.context.get("legacy"):
            return self._go(PICK_TEAM)  # back from the mentor check to the team list
        if self.state == ENROLL_SCAN and self.context.get("new"):
            user = self.context["user"]
            self._reset()
            return self._result(f"{user['username']} added", [
                f"Their ID is {user['code']}.",
                "No card yet: enroll one later with People > Enroll a card.",
            ], "success", refresh=True)
        if self.state == ENROLL_SCAN:
            return self._go(PEOPLE_MENU, {})
        if self.state == CONFIRM:
            return self._go(self.context.get("back", ADMIN_MENU), {})
        back = BACK.get(self.state)
        if back:
            # Keep what's needed a step back (e.g. the Wi-Fi list); menus start clean.
            return self._go(back, {} if back in MENUS else None)
        self._reset()
        return self.idle_screen()

    def _idle_key(self, key: str) -> Screen:
        """A key on the start screen: a team letter or digit starts typing an ID."""
        if key in self.section_keys or (key.isdigit() and self.mentors):
            self.state = USER_ID
            return self._id_key(key)
        if key == "#":
            return self._go(USER_ID, {})      # "No card? Type my ID"
        if key == "~":
            return self._go(BOARD, {})        # the Leaderboard button
        return self.idle_screen()

    # -- typing an ID (team letter + digits, or a mentor's digits)

    def _id_key(self, key: str) -> Screen | None:
        """A key while typing an ID; submits by itself once it's complete."""
        if key == "?" and self.state != USER_ID:   # admin: find them by name instead
            return self._go(NAME_SEARCH, {"for": self.state})
        if key in self.section_keys and self.buffer:
            # Another team tapped: swap the letter, keep the digits typed so far.
            self.buffer = self.section_keys[key] + "".join(c for c in self.buffer if c.isdigit())
            if len(self.buffer) == ids.code_length(self.buffer):
                return self._submit_id()
            return self._redraw()
        if not self.buffer:
            if key in self.section_keys:
                self.buffer = self.section_keys[key]
            elif key.isdigit() and self.mentors:
                self.buffer = key
            return self._redraw()  # must start with a team letter (or a mentor digit)
        if key.isdigit():
            self.buffer += key
            if len(self.buffer) == ids.code_length(self.buffer):  # e.g. 4 for A007
                return self._submit_id()
            return self._redraw()
        if key == "#" and any(c.isdigit() for c in self.buffer):  # short form: A 7 #
            return self._submit_id()
        return self._redraw()

    def _submit_id(self) -> Screen:
        """An ID was typed: look the user up, then go on to whatever needed it."""
        code, self.buffer = self.buffer, ""
        try:
            user = self.users.get_by_code(code)
        except UserError as exc:
            return self._error_keep_state(str(exc))
        return self._use_user(user, self.state)

    def _use_user(self, user: dict, purpose: str, from_person: bool = False) -> Screen:
        """Go on with ``user`` for ``purpose`` (the ID screen that asked, e.g. MOVE_ID).

        Used after typing an ID, picking a name search result, or tapping an
        action on someone's page (from_person: back returns to that page).
        """
        # Only "Deactivate / reactivate" works on someone who's switched off.
        if not user["is_active"] and purpose != ACTIVE_ID:
            return self._error_keep_state(f"{user['code']} is deactivated.")
        self.context = {"user": user, "from_person": from_person}
        # What happens next depends on which screen asked for the ID.
        if purpose == USER_ID:
            self.state = USER_MENU
            return self._user_menu_screen()
        if purpose == TOGGLE_ID:
            return self._admin_toggle(user)
        if purpose == UNCARD_ID:
            uids = [t["uid"] for t in self.users.list_tags()
                    if t["user_id"] == user["id"] and t["is_active"]]
            if not uids:
                return self._error_keep_state(f"{user['username']} has no cards.")
            return self._confirm("remove_cards", PEOPLE_MENU, user=user, uids=uids,
                                 from_person=from_person)
        if purpose == ACTIVE_ID:
            return self._confirm("toggle_active", PEOPLE_MENU, user=user,
                                 from_person=from_person)
        if purpose == RENAME_ID:
            self.state = RENAME_NAME
            self.buffer = user["username"][:MAX_NAME]   # edit the current name
            self.context["cut"] = len(user["username"]) > MAX_NAME
            return self._redraw()
        # The rest just move on to their next step.
        nxt = {ENROLL_ID: ENROLL_SCAN, MOVE_ID: PICK_TEAM,
               ADJUST_ID: ADJUST_AMOUNT}[purpose]
        return self._go(nxt)

    # -- a user's own menu

    def _user_menu_screen(self) -> Screen:
        """Someone typed their ID: show who they are and ask before signing in/out."""
        user = self.context["user"]
        try:
            stats = self.attendance.user_stats(user["id"])
        except AttendanceError as exc:
            self._reset()
            return self._result("Not found", [str(exc)], "error")
        verb = "out" if stats.signed_in else "in"
        team = self.users.team_name(stats.section)
        now = "signed in now" if stats.signed_in else "signed out now"
        return Screen(f"Sign {verb} {stats.username}?", [
            f"*  Not me      #  Sign {verb}",
        ], "prompt", view="dialog", stats=_stats(stats),
            person={"name": stats.username, "sub": f"{stats.code}  ·  {team}  ·  {now}",
                    "team": stats.section})

    def _user_menu_key(self, key: str) -> Screen | None:
        """# on the confirm screen signs them in or out (no PIN, the ID is enough)."""
        if key != "#":
            return None
        user = self.context["user"]
        self._reset()
        try:
            result = self.attendance.toggle(user["id"], method="keypad")  # a typed ID
        except SignInClosed as exc:
            return self._result("Signing in is closed", [str(exc)], "warning")
        except AttendanceError as exc:
            return self._result("Not signed in", [str(exc)], "error")
        return self._scan_screen(result.action, result.stats, result.session_seconds,
                                 result.notes)

    # -- PINs

    def _pin_key(self, key: str) -> Screen:
        """A key pressed on any PIN screen: digits are typed, # submits."""
        if key.isdigit():
            if len(self.buffer) < MAX_PIN:
                self.buffer += key
            return self._redraw()
        if key != "#":
            return self._redraw()
        # # pressed: what the PIN is for depends on the screen.
        pin, self.buffer = self.buffer, ""
        if not pin:
            return self._redraw()
        if self.state == ADMIN_PIN:
            return self._check_admin(pin)
        if self.state == SEASON_PIN:
            return self._start_season(pin)
        if self.state == ADMINPIN_OLD:  # someone at an open menu can't just change it
            if not self.users.check_admin_pin(pin):
                return self._wrong_pin()
            return self._go(ADMINPIN_NEW, {})
        # Setting a PIN: type it twice, so a typo can't lock anyone out.
        if self.state == ADMINPIN_NEW:
            if len(pin) < 4:
                return self._error_keep_state("A PIN needs 4 to 8 digits.")
            self.context["pin"] = pin
            return self._go(ADMINPIN_AGAIN)
        # ADMINPIN_AGAIN: must match the first one
        first = self.context.pop("pin", None)
        if pin != first:
            self.state = ADMINPIN_NEW
            return self._error_keep_state("The two PINs didn't match. Try again.")
        self.users.set_admin_pin(pin)
        self._reset()
        return self._result("Admin PIN changed", ["Use the new PIN from now on."], "success")

    def _check_admin(self, pin: str) -> Screen:
        """Admin PIN typed: open the admin menu (or finish a legacy mentor pick)."""
        if self.users.check_admin_pin(pin):
            self._pin_failures = 0
            if self.context.get("legacy"):  # an admin approving someone picking Mentors
                return self._set_team(self.context["section"])
            screen = self._go(ADMIN_MENU, {})
            screen.sound = "admin"
            return screen
        return self._wrong_pin()

    def _wrong_pin(self) -> Screen:
        """Count a wrong PIN; too many in a row lock the screen for a minute."""
        self._pin_failures += 1
        self._reset()
        if self._pin_failures >= MAX_PIN_FAILURES:  # stops someone guessing PINs
            self._pin_failures = 0
            self._locked_until = self.monotonic() + LOCKOUT_SECONDS
            return self._result("Locked for a minute", ["Too many wrong PINs. Try again soon."],
                                "error")
        return self._result("Wrong PIN", ["Please try again."], "error")

    # -- admin menus

    def _admin_choice(self, key: str) -> Screen | None:
        """Admin menu: 1 People, 2 Hours, 3 System."""
        menu = {"1": PEOPLE_MENU, "2": HOURS_MENU, "3": SYSTEM_MENU}.get(key)
        return self._go(menu, {}) if menu else None

    def _people_choice(self, key: str) -> Screen | None:
        """People menu: each number starts that task."""
        state = {"1": NEW_TEAM, "2": ENROLL_ID, "3": UNCARD_ID, "4": MOVE_ID,
                 "5": RENAME_ID, "6": ACTIVE_ID}.get(key)
        if key == "7":
            return self._go(OFF_LIST, {"page": 0})
        if key == "8":
            return self._go(NAME_SEARCH, {"for": PERSON})
        return self._go(state, {}) if state else None

    def _hours_choice(self, key: str) -> Screen | None:
        """Hours menu: each number starts that task."""
        if key == "1":
            return self._go(ADJUST_ID, {})
        if key == "2":
            return self._go(TOGGLE_ID, {})
        if key == "3":
            return self._go(HERE_LIST, {"page": 0})
        if key == "4":
            return self._confirm("sign_out_all", HOURS_MENU,
                                 count=len(self.attendance.currently_signed_in()))
        if key == "5":
            if self.seasons is None:
                return self._error_keep_state("Seasons can't be changed from this kiosk.")
            return self._go(SEASON_PIN, {"name": self.seasons.next_name()})
        if key == "6":
            if self.seasons is None:
                return self._error_keep_state("Seasons can't be viewed from this kiosk.")
            return self._go(PAST_LIST, {"list_page": 0})
        if key == "7":
            if self.schedule is None:
                return self._error_keep_state("Scheduled sign-out isn't set up on this kiosk.")
            return self._go(SCHEDULE, {})
        if key == "8":
            if self.attendance.sign_in_blocks is None:
                return self._error_keep_state("Sign-in blocks aren't set up on this kiosk.")
            return self._go(BLOCKS, {})
        return None

    def _system_choice(self, key: str) -> Screen | None:
        """System menu: each number starts that task."""
        # While an update installs, only info and the admin PIN stay available.
        if key in ("2", "3", "5", "6", "7", "8") and self._installing():
            return self._error_keep_state("An update is being installed. Try again after.")
        if key == "1":
            self.context["viewing"] = True  # back goes to the menu, not out of it
            return Screen("System info", self._info_lines(), "info", nav="system")
        if key == "2":
            return self._check_updates()
        if key == "3":
            return self._wifi_scan()
        if key == "4":
            return self._go(ADMINPIN_OLD, {})
        if key == "9":
            # Nothing stops: cards and typed IDs still sign people in meanwhile.
            self._reset()
            return Screen("Minimizing the kiosk", [
                "Card scans still count while it's hidden.",
                "Bring it back from the taskbar, or open NFC Kiosk",
                "from the desktop menu."], "info", hold_seconds=self.result_seconds,
                minimize_app=True)
        action = {"5": "restart_app", "6": "close_app", "7": "reboot", "8": "poweroff"}.get(key)
        if action:
            if action in ("reboot", "poweroff") and self.system is None:
                return self._error_keep_state("The Pi can't be switched off from here.")
            return self._confirm(action, SYSTEM_MENU)
        return None

    def _info_lines(self) -> list[str]:
        """Lines for System info."""
        reader = self.reader.firmware_version() if self.reader else "no reader"
        if self.system is not None:
            lines = self.system.info_lines()
        else:
            lines = [f"Host: {socket.gethostname()}", f"IP: {local_ip()}"]
        lines.append(f"NFC: {reader}")
        if self.updates is not None and self.updates.available:
            lines.append(f"Update: {self.updates.new_commits} new change(s), System > Install update")
        if self._setup_needed():
            lines.append(SETUP_REMINDER)
        return lines

    def _setup_needed(self) -> bool:
        return self.updates is not None and getattr(self.updates, "setup_needed", False)

    # -- confirmations

    def _confirm(self, action: str, back: str, **context) -> Screen:
        """Ask "are you sure?" before doing ``action``."""
        return self._go(CONFIRM, {"action": action, "back": back, **context})

    def _confirm_key(self, key: str) -> Screen | None:
        """# on a confirm screen: do the action that was asked about."""
        if key != "#":
            return None
        ctx, action = self.context, self.context["action"]
        if action == "sign_out_all":
            self._reset()
            count = self.attendance.sign_out_everyone()
            return self._result("Everyone signed out", [f"Closed {count} session(s)."],
                                "success", refresh=True)
        if action == "remove_cards":
            self._reset()
            for uid in ctx["uids"]:
                try:
                    self.users.remove_tag(uid)
                except UserError:
                    pass  # already gone
            user = ctx["user"]
            return self._result("Cards removed", [
                f"{user['username']}'s old card(s) no longer work.",
                "Their ID and hours stay. Enroll a new card with People > Enroll a card.",
            ], "success")
        if action == "toggle_active":
            return self._toggle_active(ctx["user"])
        if action == "delete_user":
            return self._delete_user(ctx["user"])
        if action == "close_app":
            self._reset()
            return Screen("Closing the kiosk", [self.reopen_hint], "warning", close_app=True)
        if action == "restart_app":
            self._reset()
            return Screen("Restarting the kiosk", ["Back in a few seconds."], "info",
                          restart_app=True)
        if action in ("reboot", "poweroff"):
            return self._power(action)
        return None

    def _toggle_active(self, user: dict) -> Screen:
        """Deactivate someone, or reactivate them if they're already off."""
        self._reset()
        try:
            if user["is_active"]:
                # Close an open session first, so they don't linger in "here now".
                if self.attendance.user_stats(user["id"]).signed_in:
                    self.attendance.toggle(user["id"], method="admin")
                self.users.set_active(user["code"], False)
                return self._result(f"{user['username']} deactivated", [
                    "Their cards and ID no longer work.",
                    "Turn them back on with People > Deactivate / reactivate.",
                ], "success", refresh=True)
            self.users.set_active(user["code"], True)
        except (UserError, AttendanceError) as exc:
            return self._result("Not changed", [str(exc)], "error")
        return self._result(f"{user['username']} reactivated",
                            [f"{user['code']} and their cards work again."], "success",
                            refresh=True)

    def _delete_user(self, user: dict) -> Screen:
        """Erase someone (their page > Delete), after the red confirm."""
        self._reset()
        try:
            self.users.delete(user["id"])
        except UserError as exc:
            return self._result("Not deleted", [str(exc)], "error")
        return self._result(f"{user['username']} deleted", [
            f"{user['code']}, their cards and their hours are gone.",
            f"The ID {user['code']} can be given to someone new.",
        ], "success", refresh=True)

    def _admin_toggle(self, user: dict) -> Screen:
        """Hours > Sign someone in or out."""
        self._reset()
        try:
            result = self.attendance.toggle(user["id"], method="admin")
        except AttendanceError as exc:
            return self._result("Not changed", [str(exc)], "error")
        return self._scan_screen(result.action, result.stats, result.session_seconds,
                                 result.notes)

    def _start_season(self, pin: str) -> Screen:
        """Admin PIN typed again: start the new season."""
        if not self.users.check_admin_pin(pin):
            return self._wrong_pin()
        name = self.context["name"]
        self._reset()
        try:
            summary = self.seasons.start_new(name)
        except SeasonError as exc:
            return self._result("Season not started", [str(exc)], "error")
        lines = [f"Season {summary.new_season} has begun. Everyone starts at 0."]
        if summary.old_season:
            lines.append(f"{summary.old_season} is saved in the archive folder.")
        if summary.signed_out:
            lines.append(f"Signed out {summary.signed_out} person(s).")
        return self._result("New season started", lines, "success", refresh=True)

    # -- people: add a user, rename

    def _new_team_key(self, key: str) -> Screen:
        """Adding a user: pick their team (1-5, or the team's letter)."""
        section = self.team_choices.get(key) or self.section_keys.get(key)
        if not section:
            return self._redraw()
        return self._go(NEW_NAME, {"section": section})

    def _text_key(self, key: str) -> Screen:
        """A key while a name or Wi-Fi password is being typed; # submits it."""
        if self.state in (NAME_SEARCH, BOARD_SEARCH):
            return self._search_key(key)
        if self.state == WIFI_FIELD:
            if key.isdigit():           # digits type too
                return self.handle_char(key)
            if key == "#":
                return self._wifi_field_done()
            return self._redraw()
        if self.state == WIFI_PASSWORD:
            if key.isdigit():           # a USB keyboard's digits type too
                return self.handle_char(key)
            if key == "#":
                password, self.buffer = self.buffer, ""
                return self._wifi_connect(self.context["ssid"], password)
            return self._redraw()
        if key != "#":
            return self._redraw()
        name = self.buffer.strip()
        if not name:
            return self._error_keep_state("Type their name first.")
        if self.state == RENAME_NAME:
            user = self.context["user"]
            if name == user["username"] or (self.context.get("cut")
                                            and name == user["username"][:MAX_NAME]):
                self._reset()   # nothing changed (and a long name isn't cut short)
                return self._result("Name not changed", [user["username"]], "info")
            try:
                self.users.rename(user["code"], name)
            except UserError as exc:
                return self._error_keep_state(str(exc))
            self._reset()
            return self._result("Renamed", [f"{user['username']} is now {name} ({user['code']})."],
                                "success", refresh=True)
        # Adding a user: create them, then offer to enroll their card straight away.
        try:
            user = self.users.add(name, self.context["section"])
        except UserError as exc:
            return self._error_keep_state(str(exc))
        screen = self._go(ENROLL_SCAN, {"user": user, "new": True})
        screen.tone, screen.sound = "success", "success"
        return screen

    # -- scheduled sign-out

    def _schedule_key(self, key: str) -> Screen | None:
        """The list of times: a number opens that time, # adds one."""
        if key == "#":
            return self._go(SCHEDULE_TIME, {})
        times = self.schedule.times()
        if key.isdigit() and 1 <= int(key) <= len(times):
            time, keep = times[int(key) - 1]
            return self._go(SCHEDULE_ITEM, {"time": time, "keep": keep})
        return None

    def _schedule_time_key(self, key: str) -> Screen:
        """Adding a time: type it as 4 digits (2100), then OK."""
        if key.isdigit():
            if len(self.buffer) < 4:
                self.buffer += key
            return self._redraw()
        if key != "#" or not self.buffer:
            return self._redraw()
        try:
            time = parse_time(self.buffer)
        except ScheduleError as exc:
            self.buffer = ""
            return self._error_keep_state(str(exc))
        if any(t == time for t, _ in self.schedule.times()):
            self.buffer = ""
            return self._error_keep_state(f"{time} is already on the list.")
        return self._go(SCHEDULE_KEEP, {"time": time})

    def _schedule_keep_key(self, key: str) -> Screen | None:
        """# keeps their hours up to then, B drops them."""
        if key not in ("#", "B"):
            return None
        time = self.context["time"]
        try:
            self.schedule.add(time, keep=key == "#")
        except ScheduleError as exc:
            self._go(SCHEDULE, {})
            return self._error_keep_state(str(exc))
        return self._go(SCHEDULE, {})

    def _schedule_item_key(self, key: str) -> Screen | None:
        """One time: # switches keep/drop, B removes it."""
        time, keep = self.context["time"], self.context["keep"]
        if key == "#":
            self.schedule.set_keep(time, not keep)
        elif key == "B":
            self.schedule.remove(time)
        else:
            return None
        return self._go(SCHEDULE, {})

    # -- sign-in blocks

    def _blocks_key(self, key: str) -> Screen | None:
        """The list of blocks: a number opens that block, # adds one."""
        if key == "#":
            if len(self.attendance.sign_in_blocks.list()) >= MAX_BLOCKS:
                return self._error_keep_state(f"Up to {MAX_BLOCKS} blocks. Remove one first.")
            return self._go(BLOCK_START, {})
        blocks = self.attendance.sign_in_blocks.list()
        if key.isdigit() and 1 <= int(key) <= len(blocks):
            return self._go(BLOCK_ITEM, {"block": blocks[int(key) - 1]})
        return None

    def _block_time_key(self, key: str) -> Screen:
        """From / until: 4 digits, then OK."""
        if key.isdigit():
            if len(self.buffer) < 4:
                self.buffer += key
            return self._redraw()
        if key != "#" or not self.buffer:
            return self._redraw()
        try:
            time = parse_time(self.buffer)
        except ScheduleError as exc:
            self.buffer = ""
            return self._error_keep_state(str(exc))
        if self.state == BLOCK_START:
            self.context["start"] = time
            return self._go(BLOCK_END)
        if time == self.context["start"]:
            self.buffer = ""
            return self._error_keep_state("It has to end at a different time.")
        self.context.update(end=time, days="12345")   # school days to start with
        return self._go(BLOCK_DAYS)

    def _block_days_key(self, key: str) -> Screen | None:
        """1-7 switch a day on or off, # saves."""
        days = self.context["days"]
        if key in "1234567" and len(key) == 1:
            self.context["days"] = (days.replace(key, "") if key in days
                                    else "".join(sorted(days + key)))
            return self._redraw()
        if key != "#":
            return None
        if not days:
            return self._error_keep_state("Pick at least one day.")
        block = Block(self.context["start"], self.context["end"], days)
        blocks = self.attendance.sign_in_blocks
        if "old" in self.context:
            blocks.replace(self.context["old"], block)
        else:
            blocks.add(block)
        return self._go(BLOCKS, {})

    def _block_item_key(self, key: str) -> Screen | None:
        """One block: # changes its days, B removes it."""
        block = self.context["block"]
        if key == "B":
            self.attendance.sign_in_blocks.remove(block)
            return self._go(BLOCKS, {})
        if key == "#":
            return self._go(BLOCK_DAYS, {"start": block.start, "end": block.end,
                                         "days": block.days, "old": block})
        return None

    # -- finding people by name

    def _search_users(self, query: str, include_inactive: bool = False) -> list[dict]:
        """People whose name (or ID) matches, best first: name starts with it, then a
        word in it does, then it's anywhere in the name."""
        words = query.lower().split()
        if not words:
            return []
        found = []
        for user in self.users.list(include_inactive=include_inactive):
            name = user["username"].lower()
            if not all(w in name or w == user["code"].lower() for w in words):
                continue
            if name.startswith(words[0]) or user["code"].lower() == words[0]:
                score = 0
            elif any(part.startswith(words[0]) for part in name.split()):
                score = 1
            else:
                score = 2
            found.append((score, name, user))
        return [user for _, _, user in sorted(found, key=lambda f: f[:2])]

    def _search_key(self, key: str) -> Screen:
        """Name search: a result's number picks it, Done (#) picks the first one."""
        found = self._search_results()
        if self.state == BOARD_SEARCH:
            if key == "#":   # Done: back to the leaderboard, showing only the matches
                return self._go(BOARD, {"query": self.buffer.strip()})
            return self._redraw()
        if key == "#" and found:
            key = "1"
        if not key.isdigit() or not 1 <= int(key) <= min(len(found), SEARCH_SHOWN):
            return self._redraw()
        user, purpose = found[int(key) - 1], self.context["for"]
        if purpose == PERSON:
            return self._open_person(user["id"], query=self.buffer)
        if not user["is_active"] and purpose != ACTIVE_ID:
            return self._error_keep_state(f"{user['username']} is deactivated.")
        self.state, self.buffer = purpose, ""
        return self._use_user(user, purpose)

    def _search_results(self) -> list[dict]:
        if self.state == BOARD_SEARCH:
            return self._search_users(self.buffer)
        # Deactivated people can be found to switch them back on, or to see their page.
        return self._search_users(self.buffer, self.context["for"] in (ACTIVE_ID, PERSON))

    def _open_person(self, user_id: int, query: str | None = None) -> Screen:
        """Someone's page (People > Find someone by name)."""
        ctx = {"user": self.users.get(user_id)}
        if query is None:   # coming back from an action: keep the search to go back to
            query = self.context.get("query", "")
        ctx["query"] = query
        return self._go(PERSON, ctx)

    def _person_key(self, key: str) -> Screen | None:
        """An action on someone's page: straight to its next step, no ID to type."""
        purpose = {"1": MOVE_ID, "2": RENAME_ID, "3": ENROLL_ID, "4": UNCARD_ID,
                   "5": ACTIVE_ID, "6": ADJUST_ID, "7": TOGGLE_ID}.get(key)
        query = self.context.get("query", "")
        if key == "8":   # Delete: works on deactivated people too
            user = self.context["user"]
            self._confirm("delete_user", PEOPLE_MENU, user=user, from_person=True)
            self.context["query"] = query
            return self._redraw()
        if purpose is None:
            return None
        screen = self._use_user(self.context["user"], purpose, from_person=True)
        if self.state != IDLE:          # (signing in or out finishes straight away)
            self.context["query"] = query   # for back, through the action, to the search
        return screen

    # -- the leaderboard

    def _board_key(self, key: str) -> Screen | None:
        """The leaderboard: ? searches by name, # shows everyone again."""
        if key == "?":
            return self._go(BOARD_SEARCH, {})
        if key == "#" and self.context.get("query"):
            return self._go(BOARD, {})
        return None

    def _board_rows(self, entries, users: list[dict] | None = None) -> list[dict]:
        """Leaderboard entries as rows (only those in ``users``, if given)."""
        if users is not None:
            ids_wanted = [u["id"] for u in users]
            by_id = {e.user_id: e for e in entries}
            entries = [by_id[i] for i in ids_wanted if i in by_id]
        return [{"name": e.username, "team": e.section,
                 "rank": e.rank if e.total_seconds else None,   # no time yet: no rank
                 "sub": f"{e.code}  ·  {self.users.team_name(e.section, short=True)}",
                 "right": timefmt.format_duration(e.total_seconds) if e.total_seconds
                 else "No time yet"} for e in entries]

    def _here_list_key(self, key: str) -> Screen | None:
        """# on the "who is here" / "deactivated" lists: next page."""
        if key != "#":
            return None
        self.context["page"] += 1        # wraps round in _redraw
        return self._redraw()

    def _past_list_key(self, key: str) -> Screen | None:
        """Past seasons: 1-8 opens that season, # shows the next page of seasons."""
        if key == "#":
            self.context["list_page"] += 1   # wraps round in _redraw
            return self._redraw()
        shown = self.context.get("shown", [])
        if key.isdigit() and 1 <= int(key) <= len(shown):
            self.context.update(season=shown[int(key) - 1], page=0)
            return self._go(PAST_SEASON)
        return None

    # -- system: background jobs

    def _background(self, screen: Screen, job: Callable[[], object],
                    always: bool = False) -> Screen:
        """Run a slow job off the touch thread; its result is published later."""
        # The job returns a result Screen, None (nothing to show), or a function
        # that moves on to the next step's screen. A card scan meanwhile cancels
        # the menu and the result is dropped, unless ``always`` is set.
        token = object()  # identifies this job, so a stale one can tell it's stale
        self.state, self.buffer, self._job = BUSY, "", token

        def work():
            try:
                result = job()
            except Exception as exc:
                log.exception("kiosk job failed")
                result = self._result("Something went wrong", [str(exc)], "error")
            with self._lock:
                # Still ours if no card scan or newer job replaced it meanwhile.
                owned = self._job is token and self.state == BUSY
                if self._job is token:
                    self._job = None
                if owned:
                    # The job's time doesn't count as idle: its next screen gets the
                    # full idle timeout.
                    self._last_input = self.monotonic()
                if not owned and not always:
                    return
                if callable(result):
                    result = result() if owned else None
                elif owned:
                    self._reset()
            if result is not None and self.publish:
                self.publish(result)

        self._spawn(work)
        screen.busy = True
        return screen

    def _check_updates(self) -> Screen:
        """System > Check for updates: check GitHub, then offer to install what's new."""
        if self.updates is None:
            return self._error_keep_state("Updates aren't set up on this kiosk.")

        def job():
            try:
                count = self.updates.check(wait=True)
            except UpdateError as exc:
                return self._result("Couldn't check for updates", [str(exc)], "warning")
            if count is None:
                return self._result("Couldn't check for updates", [
                    "The Pi isn't online, or this copy wasn't installed with git.",
                    "Connect to Wi-Fi with System > Wi-Fi.",
                ], "warning")
            if count == 0:
                version = self.system.version() if self.system else ""
                return self._result("Up to date", [f"Version {version}" if version else
                                                   "This is the newest version."], "success")
            changes = self.updates.pending_changes()
            return lambda: self._go(UPDATE_CONFIRM, {"count": count, "changes": changes})

        return self._background(Screen("Checking for updates…", ["One moment."], "info"), job)

    def _update_confirm_key(self, key: str) -> Screen | None:
        """# on the update screen: install it, then restart the app."""
        if key != "#":
            return None

        def job():
            try:
                result = self.updates.install()
            except UpdateError as exc:
                return self._result("Update not installed", [str(exc)], "error")
            return Screen("Update installed", [
                f"Version {result.old} → {result.new}", *result.notes, "",
                "Restarting the kiosk…",
            ], "success", restart_app=True)

        return self._background(Screen("Installing the update…", [
            "Please don't turn off the Pi.",
            "The kiosk restarts by itself when it's done.",
        ], "warning"), job, always=True)

    def _wifi_scan(self) -> Screen:
        """System > Wi-Fi: list nearby networks."""
        if self.system is None:
            return self._error_keep_state("Wi-Fi can't be set up from this kiosk.")

        def job():
            try:
                try:
                    networks = self.system.wifi_networks()
                except WifiOffError:
                    # They asked for Wi-Fi, so switch the radio on and look again.
                    self.system.wifi_on()
                    self._sleep(WIFI_ON_SECONDS)
                    networks = self.system.wifi_networks()
            except SystemActionError as exc:
                return self._result("Wi-Fi", [str(exc)], "error")
            networks = networks[:MAX_WIFI_LIST]
            return lambda: self._go(WIFI_LIST, {"networks": networks})

        return self._background(Screen("Looking for Wi-Fi…", ["One moment."], "info"), job)

    def _wifi_list_key(self, key: str) -> Screen | None:
        """Pick a network (# scans again)."""
        if key == "#":
            return self._wifi_scan()
        networks = self.context["networks"]
        if not key.isdigit() or not 1 <= int(key) <= len(networks):
            return None
        network = networks[int(key) - 1]
        if network.enterprise:          # username first, then the password
            self.context = {"networks": networks, "ssid": network.ssid,
                            "login": EnterpriseLogin()}
            return self._wifi_field("username", first=True)
        if network.secured:
            return self._go(WIFI_PASSWORD, {"networks": networks, "ssid": network.ssid})
        return self._wifi_connect(network.ssid, None)

    def _wifi_connect(self, ssid: str, password: str | None,
                      login: EnterpriseLogin | None = None) -> Screen:
        """Join the network in the background. If it fails: back to the list, or to
        the enterprise sign-in screen (with what was typed) to fix it."""
        networks = self.context.get("networks", [])
        context = dict(self.context)

        def job():
            try:
                if login is not None:
                    ip = self.system.wifi_connect_enterprise(ssid, login)
                else:
                    ip = self.system.wifi_connect(ssid, password)
            except SystemActionError as exc:
                def back_to_list(message=str(exc)):
                    if login is not None:
                        self._go(WIFI_LOGIN, context)
                    else:
                        self._go(WIFI_LIST, {"networks": networks})
                    return self._error_keep_state(message)
                return back_to_list
            if self.updates is not None and getattr(self.updates, "automatic", False):
                self._spawn(self.updates.check)   # now online: look for updates
            return self._result("Wi-Fi connected", [f"Connected to {ssid}."]
                                + ([f"IP: {ip}"] if ip else []), "success")

        return self._background(Screen(f"Connecting to {ssid}…", ["This can take 30 seconds."],
                                       "info"), job)

    def _wifi_field(self, name: str, first: bool = False) -> Screen:
        """Type one of the enterprise sign-in's fields (the old value to edit; not
        the password, which is typed again or left empty to keep it)."""
        self.context.update(field=name, first=first)
        self.state = WIFI_FIELD
        attr = WIFI_FIELDS[name][0]
        self.buffer = "" if name == "password" else getattr(self.context["login"], attr)
        return self._redraw()

    def _wifi_field_done(self) -> Screen:
        """# on a field: keep it. The first time through, the password comes next."""
        ctx = self.context
        login, name = ctx["login"], ctx["field"]
        if name == "password":
            if self.buffer:             # empty: keep the one typed before
                login.password = self.buffer
        else:
            setattr(login, WIFI_FIELDS[name][0], self.buffer.strip())
        if ctx.pop("first", False) and name == "username":
            return self._wifi_field("password", first=True)
        return self._go(WIFI_LOGIN)

    def _wifi_login_key(self, key: str) -> Screen | None:
        """The enterprise sign-in screen: pick a line to change it, # connects."""
        login: EnterpriseLogin = self.context["login"]
        if key == "#":
            if not login.username:
                return self._error_keep_state("Type the username first.")
            if not login.password:
                return self._error_keep_state("Type the password first.")
            return self._wifi_connect(self.context["ssid"], None, login)
        if key == "3":                  # next sign-in method
            methods = list(EAP_METHODS)
            login.method = methods[(methods.index(login.method) + 1) % len(methods)
                                   if login.method in methods else 0]
            return self._redraw()
        if key == "5":
            login.check_cert = not login.check_cert
            return self._redraw()
        field = {"1": "username", "2": "password", "4": "anon"}.get(key)
        if key == "6" and login.check_cert:
            field = "domain"
        return self._wifi_field(field) if field else None

    def _power(self, action: str) -> Screen:
        """Restart or shut down the Pi, after a moment to read the screen."""
        def job():
            self._sleep(POWER_DELAY_SECONDS)
            try:
                self.system.power(action)
            except SystemActionError as exc:
                return self._result("Not done", [str(exc)], "error")
            return None   # the Pi is going down

        if action == "poweroff":
            screen = Screen("Shutting down", [
                "Wait until the green light on the Pi stops flashing,",
                "then it's safe to unplug it.",
            ], "warning")
        else:
            screen = Screen("Restarting the Pi", ["The kiosk is back in about a minute."],
                            "warning")
        return self._background(screen, job, always=True)

    def _amount_key(self, key: str) -> Screen:
        """Hours > Add / subtract: type the time, then Add (A) or Subtract (B)."""
        if key.isdigit():
            if len(self.buffer) < MAX_AMOUNT:
                self.buffer += key
            return self._redraw()
        if key in ("A", "B"):
            try:
                seconds = parse_amount(self.buffer)
            except ValueError as exc:
                self.buffer = ""
                return self._error_keep_state(str(exc))
            if key == "B":
                seconds = -seconds
            user = self.context["user"]
            try:
                stats = self.attendance.adjust(user["id"], seconds, "kiosk", via="kiosk")
            except AttendanceError as exc:
                self.buffer = ""
                return self._error_keep_state(str(exc))
            self._reset()
            verb = "Added" if seconds > 0 else "Subtracted"
            return self._result(f"{verb} {timefmt.format_duration(abs(seconds))}", [
                f"{stats.username} ({stats.code})",
                f"New season total: {stats.total_text}",
                f"Leaderboard rank: {_rank_text(stats)}",
            ], "success", refresh=True)
        return self._redraw()

    # ------------------------------------------------------------ teams

    def _pick_team_key(self, key: str) -> Screen:
        """A team picked by number or letter (legacy card's owner, or an admin)."""
        section = self.team_choices.get(key) or self.section_keys.get(key)
        if not section:
            return self._redraw()
        if self.context.get("legacy") and section == ids.MENTORS:
            # People choose their own team, but only an admin can make a mentor or captain.
            if not self.users.admin_pin_set():
                return self._error_keep_state("Mentors and captains need an admin, and no admin PIN is set.")
            self.state = ADMIN_PIN
            self.context["section"] = section
            return self._redraw()
        return self._set_team(section)

    def _set_team(self, section: str) -> Screen:
        """Move the user to the team; a legacy card's owner is then signed in."""
        uid, user = self.context.get("uid"), self.context["user"]
        self._reset()
        team = self.users.team_name(section)
        try:
            user = self.users.move(user["id"], section)
            if uid is None:
                return self._result("Team changed", [
                    f"{user['username']} is now in {team}.",
                    f"Their ID is now {user['code']}.",
                ], "success", refresh=True)
            result = self.attendance.scan_tag(uid)
        except (UserError, AttendanceError) as exc:
            return self._result("Team not changed", [str(exc)], "error")
        # The card has usually left the reader by now; it's written on the next scan.
        notes = [f"Now in {team}. Your ID is {user['code']} if you ever need to type it."]
        return self._scan_screen(result.action, result.stats, result.session_seconds,
                                 notes + result.notes)

    # ------------------------------------------------------------ drawing

    def _redraw(self) -> Screen:
        """Build the Screen for the current state, without changing anything."""
        screen = self._draw()
        if screen.view == "page":   # how the page lays it out, from the state
            screen.view = VIEWS.get(self.state, "page")
        if screen.view == "menu":
            screen.nav = NAV[self.state]
        return screen

    def _draw(self) -> Screen:
        """The Screen for the current state (see _redraw)."""
        state, ctx = self.state, self.context
        if state == ADMIN_MENU:
            lines = ["1  People: Find people by name, add people, enroll cards, change teams",
                     f"2  Hours: {self._here_count()}Adjust hours, sign people in or out, scheduled sign-out, seasons",
                     "3  System: Wi-Fi, updates, admin PIN, restart, shut down"]
            if self.updates is not None and self.updates.available:
                lines.append(f"Update ready: {self.updates.new_commits} new")
            return Screen("Admin", lines, "prompt")
        if state == PEOPLE_MENU:
            return Screen("People", list(PEOPLE_MENU_LINES), "prompt")
        if state == HOURS_MENU:
            return Screen("Hours", list(HOURS_MENU_LINES), "prompt")
        if state == SYSTEM_MENU:
            update = "2  Check for updates"
            if self.updates is not None and self.updates.available:
                update = f"2  Install update ({self.updates.new_commits} new)"
            lines = [
                "1  System info", update, "3  Wi-Fi", "4  Change the admin PIN",
                "5  Restart the kiosk app", "6  Close the kiosk app",
                "7  Restart the Pi", "8  Shut down the Pi", "9  Minimize the kiosk app",
            ]
            if self._setup_needed():
                lines.insert(0, SETUP_REMINDER)
            return Screen("System", lines, "prompt")
        if state == USER_MENU:
            return self._user_menu_screen()
        if state == CONFIRM:
            return self._confirm_screen()
        if state == PICK_TEAM:
            user = ctx["user"]
            legacy = ctx.get("legacy")
            choices = [f"{key}  {self.users.team_name(letter)}"
                       + (" / Captains  (needs an admin)" if legacy and letter == ids.MENTORS else "")
                       for key, letter in self.team_choices.items()]
            if legacy:
                return Screen(f"Welcome, {user['username']}!", [
                    "Please choose your team before signing in.",
                    "Your old card still works. This only happens once.",
                    *choices, "*  Cancel (not signed in)",
                ], "prompt")
            cancel = ["*  Cancel (they won't be signed in)"] if "uid" in ctx else []
            return Screen(f"Team for {user['username']}", [
                f"Currently: {self.users.team_name(user['section'])} ({user['code']})",
                *choices, *cancel,
            ], "prompt")
        if state == ADMIN_PIN and ctx.get("legacy"):
            user = ctx["user"]
            return Screen(f"{user['username']} as a mentor or captain", [
                "An admin needs to confirm this.",
                "Admin: type the PIN, then tap OK.",
            ], "prompt", entry="•" * len(self.buffer), keyboard="numpad")
        if state == NEW_TEAM:
            choices = [f"{key}  {self.users.team_name(letter)}"
                       for key, letter in self.team_choices.items()]
            return Screen("Add a person: team", choices, "prompt")
        if state == NAME_SEARCH:
            found = self._search_results()
            what = {PERSON: "Find someone", ENROLL_ID: "Enroll a card", ADJUST_ID: "Adjust hours",
                    MOVE_ID: "Change team", UNCARD_ID: "Remove cards", RENAME_ID: "Rename",
                    ACTIVE_ID: "Deactivate / reactivate",
                    TOGGLE_ID: "Sign in or out"}[ctx["for"]]
            rows = [{"key": str(n), "name": u["username"], "team": u["section"],
                     "sub": f"{u['code']}  ·  {self.users.team_name(u['section'], short=True)}"
                            + ("" if u["is_active"] else "  ·  deactivated")}
                    for n, u in enumerate(found[:SEARCH_SHOWN], start=1)]
            return Screen(f"{what}: search by name", self._search_lines(found), "prompt",
                          entry=self.buffer + "_", keyboard="name", rows=rows)
        if state == BOARD_SEARCH:
            found = self._search_results()
            rows = self._board_rows(self.attendance.leaderboard(), found[:SEARCH_SHOWN])
            return Screen("Leaderboard: find a rank", self._search_lines(found), "prompt",
                          entry=self.buffer + "_", keyboard="name", rows=rows)
        if state == BOARD:
            entries = self.attendance.leaderboard()
            query = ctx.get("query", "")
            season = self.attendance.active_season_name()
            if query:
                found = self._search_users(query)
                rows = self._board_rows(entries, found)
                return Screen("Leaderboard", [
                    f"Season {season}  ·  \u201c{query}\u201d: {len(rows)} found",
                    "?  Search again      #  Show everyone"], "info", rows=rows)
            people = f"{len(entries)} {'person' if len(entries) == 1 else 'people'}"
            return Screen("Leaderboard", [f"Season {season}  ·  {people}",
                                          "?  Find someone's rank"], "info",
                          rows=self._board_rows(entries))
        if state == BLOCKS:
            blocks = self.attendance.sign_in_blocks.list()
            lines = ["Nobody can sign in at these times. Signing out still works, and admins "
                     "can still sign people in (Hours > Sign someone in or out)."]
            lines += [f"{n}  {b.start} to {b.end}  ·  {b.days_text}"
                      for n, b in enumerate(blocks, start=1)]
            if not blocks:
                lines += ["", "No blocks yet."]
            closed = self.attendance.sign_in_blocks.blocking(self.attendance.clock())
            if closed:
                lines += ["", f"Signing in is closed right now (until {closed.end})."]
            lines += ["", "#  Add a block"]
            return Screen("Sign-in blocks", lines, "prompt")
        if state in (BLOCK_START, BLOCK_END):
            digits = self.buffer.ljust(4, "_") if len(self.buffer) < 4 else self.buffer
            if state == BLOCK_START:
                title, what = "New block: from", "When does it start?"
            else:
                title, what = f"New block: {ctx['start']} until", "When does it end?"
            return Screen(title, [what, "24-hour clock: 0900 = 9:00 AM,  1530 = 3:30 PM"],
                          "prompt", entry=f"{digits[:2]}:{digits[2:]}", keyboard="numpad")
        if state == BLOCK_DAYS:
            days = ctx["days"]
            lines = [f"{ctx['start']} to {ctx['end']}: which days?  ({days_text(days)})"]
            lines += [f"{n}  {'✓' if str(n) in days else '○'}  {name}"
                      for n, name in enumerate(DAY_NAMES_LONG, start=1)]
            lines += ["", "#  Save"]
            return Screen("Sign-in block: days", lines, "prompt")
        if state == BLOCK_ITEM:
            block = ctx["block"]
            return Screen(f"Block {block.start} to {block.end}", [
                f"Days: {block.days_text}",
                "*  Cancel      B  Remove      #  Change days",
            ], "prompt")
        if state == SCHEDULE:
            times = self.schedule.times()
            lines = ["Everyone still signed in is signed out at these times, every day."]
            lines += [f"{n}  {t}  ·  {'their hours count' if keep else 'hours not counted'}"
                      for n, (t, keep) in enumerate(times, start=1)]
            if not times:
                lines += ["", "No times yet."]
            lines += ["", "#  Add a time"]
            return Screen("Scheduled sign-out", lines, "prompt")
        if state == SCHEDULE_TIME:
            digits = self.buffer
            shown = (digits.rjust(4, "_") if len(digits) == 4 else digits.ljust(4, "_"))
            return Screen("Add a sign-out time", [
                "Type the time on the 24-hour clock:",
                "2100 = 9:00 PM,  1530 = 3:30 PM",
            ], "prompt", entry=f"{shown[:2]}:{shown[2:]}", keyboard="numpad")
        if state == SCHEDULE_KEEP:
            return Screen(f"At {ctx['time']}, count their hours?", [
                "Everyone still signed in is signed out then.",
                "Count: their time up to then is kept, like signing out.",
                "Don't count: that visit earns nothing (for people who forget).",
                "*  Cancel      B  Don't count      #  Count them",
            ], "prompt")
        if state == SCHEDULE_ITEM:
            now = "Their hours up to then count." if ctx["keep"] else "Their hours aren't counted."
            switch = "Don't count them" if ctx["keep"] else "Count them"
            return Screen(f"Sign-out at {ctx['time']}", [
                now, f"*  Cancel      B  Remove      #  {switch}",
            ], "prompt")
        if state == PERSON:
            user = ctx["user"]
            stats = self.attendance.user_stats(user["id"])
            cards = sum(1 for t in self.users.list_tags()
                        if t["user_id"] == user["id"] and t["is_active"])
            team = self.users.team_name(user["section"])
            now = "signed in now" if stats.signed_in else "signed out"
            if not user["is_active"]:
                now = "deactivated"
            switch = "5  Deactivate" if user["is_active"] else "5  Reactivate"
            return Screen(user["username"], [
                "1  Change team", "2  Rename", "3  Enroll a card",
                f"4  Remove cards ({cards})", switch, "6  Add / subtract hours",
                f"7  Sign {'out' if stats.signed_in else 'in'}", "8  Delete",
            ], "prompt", stats=_stats(stats) + [("Cards", str(cards))],
                person={"name": user["username"], "team": user["section"],
                        "sub": f"{user['code']}  ·  {team}  ·  {now}"})
        if state in NAME_STATES:
            if state == NEW_NAME:
                title = f"Add a person  ·  {self.users.team_name(ctx['section'])}"
            else:
                title = f"Rename {ctx['user']['code']}"
            lines = ["Their name"]
            if state == RENAME_NAME and ctx.get("cut"):
                lines.insert(0, f"Their name is longer than {MAX_NAME} letters: it will be "
                                "shortened if you change it.")
            return Screen(title, lines, "prompt", entry=self.buffer + "_", keyboard="name")
        if state == WIFI_PASSWORD:
            # Hide the password except its last character, like a phone does.
            shown = "•" * max(len(self.buffer) - 1, 0) + self.buffer[-1:]
            return Screen(f"Password for {ctx['ssid']}", ["Wi-Fi password"],
                          "prompt", entry=shown + "_", keyboard="text")
        if state == WIFI_FIELD:
            name = ctx["field"]
            title = WIFI_FIELDS[name][1]
            lines = [title]
            if name == "password":
                shown = "•" * max(len(self.buffer) - 1, 0) + self.buffer[-1:]
                if ctx["login"].password and not ctx.get("first"):
                    lines.insert(0, "Leave it empty to keep the password typed before.")
            else:
                shown = self.buffer
                if name == "anon":
                    lines.insert(0, "Only if the network asks for one, e.g. anonymous@school.ca.")
                elif name == "domain":
                    lines.insert(0, "The server's name must end with this, e.g. school.ca.")
            return Screen(f"{title} for {ctx['ssid']}", lines, "prompt", entry=shown + "_",
                          keyboard="text")
        if state == WIFI_LOGIN:
            login = ctx["login"]
            dots = "•" * min(len(login.password), 12)
            lines = [f"1  Username: {_squeeze(login.username, 26) or 'not set'}",
                     f"2  Password: {dots or 'not set'}",
                     f"3  Sign-in method: {EAP_METHODS.get(login.method, EAP_METHODS['peap'])[0]}",
                     f"4  Anonymous identity: {_squeeze(login.anonymous_identity, 20) or 'none'}",
                     f"5  Check the server certificate: {'yes' if login.check_cert else 'no'}"]
            if login.check_cert:
                lines.append(f"6  Server domain: {_squeeze(login.domain, 24) or 'any'}")
            return Screen(f"Sign in to {ctx['ssid']}", lines + ["", "#  Connect"], "prompt")
        if state == WIFI_LIST:
            networks = ctx["networks"]
            # Spaces squeezed: 3 in a row would split the line's button in two.
            lines = [f"{n}  {_cut(' '.join(net.ssid.split()), 22)}  {net.signal}%"
                     + f"  {net.kind}"
                     + ("  (connected)" if net.in_use else "")
                     for n, net in enumerate(networks, start=1)]
            return Screen("Wi-Fi networks", (lines or ["No networks found."])
                          + ["", "#  Scan again"], "prompt")
        if state == HERE_LIST:
            rows = self.attendance.currently_signed_in()
            pages = max(1, -(-len(rows) // HERE_PAGE))  # round up
            page = ctx["page"] % pages                  # past the last page: back to the first
            ctx["page"] = page
            lines = [f"{r['code']}  {_cut(r['username'], 22)}  since {r['sign_in_at']:%H:%M}"
                     for r in rows[page * HERE_PAGE:(page + 1) * HERE_PAGE]]
            footer = [f"Page {page + 1} of {pages}", "#  Next page"] if pages > 1 else []
            return Screen(f"Here now: {len(rows)}", (lines or ["Nobody"]) + footer, "info")
        if state == OFF_LIST:
            rows = [u for u in self.users.list(include_inactive=True) if not u["is_active"]]
            pages = max(1, -(-len(rows) // HERE_PAGE))
            page = ctx["page"] % pages
            ctx["page"] = page
            lines = [f"{u['code']}  {_cut(u['username'], 30)}"
                     for u in rows[page * HERE_PAGE:(page + 1) * HERE_PAGE]]
            footer = ([f"Page {page + 1} of {pages}", "#  Next page"] if pages > 1 else [])
            footer = ["", "Reactivate with People > Deactivate / reactivate.", *footer]
            return Screen(f"Deactivated: {len(rows)}", (lines or ["Nobody"]) + footer, "info")
        if state == PAST_LIST:
            seasons = self.seasons.past()
            pages = max(1, -(-len(seasons) // HERE_PAGE))
            page = ctx["list_page"] % pages
            ctx["list_page"] = page
            ctx["shown"] = seasons[page * HERE_PAGE:(page + 1) * HERE_PAGE]
            lines = [f"{n}  {_cut(x['name'], 20)}  {_season_dates(x)}"
                     for n, x in enumerate(ctx["shown"], start=1)]
            if not seasons:
                lines = ["None yet. A season is saved here when",
                         "a new one starts (Hours > Start a new season)."]
            footer = ([f"Page {page + 1} of {pages}", "#  More seasons"] if pages > 1 else [])
            return Screen(f"Past seasons: {len(seasons)}", lines + footer,
                          "prompt" if seasons else "info")
        if state == PAST_SEASON:
            season = ctx["season"]
            rows = self.seasons.records(season["id"])
            total = timefmt.format_duration(sum(e.total_seconds for e in rows))
            pages = max(1, -(-len(rows) // HERE_PAGE))
            page = ctx["page"] % pages
            ctx["page"] = page
            lines = [f"{e.rank:>2}. {e.code}  {_cut(e.username, 20)}  "
                     f"{timefmt.format_duration(e.total_seconds)}"
                     for e in rows[page * HERE_PAGE:(page + 1) * HERE_PAGE]]
            footer = ([f"Page {page + 1} of {pages}  ·  {total} in all", "#  Next page"]
                      if pages > 1 else ["", f"{total} in all"])
            people = f"{len(rows)} {'person' if len(rows) == 1 else 'people'}"
            return Screen(f"Season {season['name']}: {people}",
                          (lines or ["Nobody has time in this season."]) + footer, "info")
        if state == UPDATE_CONFIRM:
            count = ctx["count"]
            lines = [f"•  {change[:44]}" for change in ctx["changes"][:4]]
            if count > len(lines):
                lines.append(f"   ... and {count - len(lines)} more")
            return Screen(f"Update: {count} new change{'s' if count != 1 else ''}", [
                *lines, "",
                "The kiosk restarts after installing.",
                "*  Not now      #  Install now",
            ], "prompt")
        if state == ENROLL_SCAN and ctx.get("new"):
            user = ctx["user"]
            return Screen(f"Added {user['username']}", [
                f"Their ID is {user['code']}.",
                "Hold their card on the reader now to enroll it.",
                "*  Skip (no card for now)",
            ], "prompt")
        if state == ENROLL_SCAN:
            user = ctx["user"]
            return Screen("Tap the new card",
                          [f"Hold it on the reader to enroll it for {user['username']} "
                           f"({user['code']}).", "*  Cancel"], "prompt")
        if state in ID_STATES:
            title = {USER_ID: "Type your ID", ENROLL_ID: "Enroll a card: user ID",
                     ADJUST_ID: "Adjust hours: user ID", MOVE_ID: "Change team: user ID",
                     UNCARD_ID: "Remove cards: user ID", RENAME_ID: "Rename: user ID",
                     ACTIVE_ID: "Deactivate / reactivate: user ID",
                     TOGGLE_ID: "Sign in or out: user ID"}[state]
            # Show the typed part plus a _ for each character still to come: A0__
            length = ids.code_length(self.buffer) if self.buffer else ids.DIGITS + 1
            shown = self.buffer + "_" * (length - len(self.buffer))
            lines = ["Tap the team, then type the number."]
            if state != USER_ID:   # admins can also find people (and imported ones) by name
                lines.append("?  Search by name")
            return Screen(title, lines, "prompt", entry=shown, keyboard="id")
        if state in PIN_STATES:
            return self._pin_screen()
        if state == ADJUST_AMOUNT:
            user = ctx["user"]
            try:
                amount = timefmt.format_duration(parse_amount(self.buffer))
            except ValueError:
                amount = "?"
            return Screen(f"Adjust {user['username']} ({user['code']})", [
                "Type the time as hours then minutes:",
                "130 = 1h 30m,  45 = 45m,  200 = 2h",
                "",
                "B  Subtract      A  Add",
            ], "prompt", entry=f"{self.buffer or '0'}  =  {amount}", keyboard="numpad")
        return self.idle_screen()

    def _pin_screen(self) -> Screen:
        """Every PIN screen, with the number pad."""
        screen = self._pin_screen_text()
        screen.keyboard = "numpad"
        return screen

    def _pin_screen_text(self) -> Screen:
        """The PIN screens' text; the PIN shows as dots."""
        state, ctx = self.state, self.context
        dots = "•" * len(self.buffer)
        if state == ADMINPIN_OLD:
            return Screen("Change admin PIN", ["Type the current admin PIN."],
                          "prompt", entry=dots)
        if state == ADMINPIN_NEW:
            return Screen("New admin PIN", ["Type the new PIN (4 to 8 digits)."],
                          "prompt", entry=dots)
        if state == ADMINPIN_AGAIN:
            return Screen("Type the PIN again", ["The same new PIN, to be sure."], "prompt",
                          entry=dots)
        if state == SEASON_PIN:
            season = self.attendance.active_season_name()
            return Screen(f"Start season {ctx['name']}?", [
                "Everyone's hours go back to 0.",
                f"Season {season} is saved to the archive,",
                "and everyone signed in is signed out.",
                "",
                "Type the admin PIN again to start it.",
            ], "warning", entry=dots)
        return Screen("Enter the admin PIN", ["4 to 8 digits"], "prompt", entry=dots)

    def _confirm_screen(self) -> Screen:
        """The "are you sure?" screen for each action."""
        ctx = self.context
        action = ctx["action"]
        if action == "sign_out_all":
            return Screen("Sign everyone out?", [
                f"{ctx['count']} signed in right now. Their time so far counts.",
                "", "*  Cancel      #  Sign them out"], "warning")
        if action == "remove_cards":
            user, uids = ctx["user"], ctx["uids"]
            shown = [f"   {uid}" for uid in uids[:3]]
            if len(uids) > 3:
                shown.append(f"   ... and {len(uids) - 3} more")
            return Screen(f"Remove {user['username']}'s cards?", [
                f"These stop working ({user['code']} and their hours stay):", *shown,
                "", "*  Cancel      #  Remove"], "warning")
        if action == "toggle_active":
            user = ctx["user"]
            if user["is_active"]:
                return Screen(f"Deactivate {user['username']}?", [
                    "Their cards and ID stop working and they leave",
                    "the leaderboard. Their history is kept.",
                    "", "*  Cancel      #  Deactivate"], "warning")
            return Screen(f"Reactivate {user['username']}?", [
                f"{user['code']} and their cards work again.",
                "", "*  Cancel      #  Reactivate"], "prompt")
        if action == "delete_user":
            user = ctx["user"]
            return Screen(f"Delete {user['username']} for good?", [
                "Their ID, cards and hours in every season are erased.",
                "This can't be undone. To keep their hours, deactivate them instead.",
                "", "*  Cancel      #  Delete"], "error")
        title, lines = {
            "restart_app": ("Restart the kiosk app?", ["It's back in a few seconds."]),
            "close_app": ("Close the kiosk app?", [
                "The Pi's desktop will show instead.",
                "Card scans won't be counted until it's open again."]),
            "reboot": ("Restart the Pi?", ["The kiosk is back in about a minute."]),
            "poweroff": ("Shut down the Pi?", [
                "Do this before unplugging it.",
                "To turn it on again, unplug it and plug it back in."]),
        }[action]
        return Screen(title, [*lines, "", f"*  Cancel      #  {CONFIRM_VERBS[action]}"],
                      "warning")

    def _search_lines(self, found: list) -> list[str]:
        """The line under a name search: a hint, or how many more matched."""
        if not self.buffer.strip():
            return ["Type part of a name (or an ID)."]
        if not found:
            return ["Nobody by that name."]
        if len(found) > SEARCH_SHOWN:
            return [f"{len(found) - SEARCH_SHOWN} more: keep typing to narrow it down."]
        return [""]

    def _error_keep_state(self, message: str, screen: Screen | None = None) -> Screen:
        """Stay on this screen, with the error on top in red."""
        screen = screen or self._redraw()
        screen.lines = [message] + screen.lines
        screen.tone = "error"
        return screen


def _add_name_char(buffer: str, char: str) -> str:
    """Type one character of a name: capital at the start of each word, no double spaces."""
    if len(buffer) >= MAX_NAME:
        return buffer
    if not buffer or buffer[-1] in " -":
        char = char.upper()
    if char == " " and (not buffer or buffer.endswith(" ")):  # no leading or double spaces
        return buffer
    return buffer + char


def _stats(stats: UserStats) -> list[tuple[str, str]]:
    """The two cards on the scan and "Sign in?" screens."""
    return [(f"Season {stats.season_name}", stats.total_text),
            ("Leaderboard", _rank_text(stats))]


def _season_dates(season: dict) -> str:
    """When a season ran, e.g. "Sep 2025 - Jun 2026"."""
    start = f"{season['started_at']:%b %Y}"
    end = f"{season['ended_at']:%b %Y}" if season.get("ended_at") else "?"
    return start if end == start else f"{start} - {end}"


def _rank_text(stats: UserStats) -> str:
    """"#3 of 40", or "-" with no time yet."""
    if not stats.rank or not stats.total_seconds:   # no time yet: not ranked
        return "-"
    return f"#{stats.rank} of {stats.ranked_users}"


def local_ip() -> str:
    """This Pi's network address (same trick as system.local_ip)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "unknown"


def _squeeze(text: str, limit: int) -> str:
    """One line of typed text for a button line: no runs of spaces (3 in a row would
    split the button in two), shortened to fit."""
    return _cut(" ".join(text.split()), limit)


def _cut(text: str, limit: int) -> str:
    """Shorten text to fit, ending with …"""
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
