# rpi-nfc-tablet: touch NFC sign-in kiosk for a Raspberry Pi 4 B
# Created by LunarcatOwO (https://github.com/LunarcatOwO)
# Copyright (C) 2026 LunarcatOwO
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. It comes WITHOUT ANY WARRANTY; see the LICENSE file.

"""Name search, people's pages, the leaderboard screen, pop-ups, scheduled sign-out
and clock jumps."""

from __future__ import annotations

from datetime import timedelta

from nfc_login.db import repository as repo
from nfc_login.kiosk import controller as kc
from nfc_login.services.clockguard import ClockGuard
from nfc_login.services.schedule import AutoSignOut, ScheduleError, parse_time

from tests.test_admin_menu import make, press


def type_text(controller, text):
    screen = None
    for char in text:
        screen = controller.handle_char(char) or screen
    return screen


def add_imported(db, name, number):
    """Someone brought over from the old system, with no team yet (U001...)."""
    with db.transaction() as cur:
        cur.execute("INSERT INTO users (section, number, username, is_active, created_at) "
                    "VALUES ('U', %s, %s, 1, NOW())", (number, name))
        return cur.lastrowid


def test_find_someone_by_name_and_edit_an_imported_person(services, db):
    controller, users, *_ = make(services)
    users.add("Alex Chen", "A")
    users.add("Sam Rivera", "B")
    add_imported(db, "Avery Santos", 1)
    screen = press(controller, "*2468#18")
    assert controller.state == kc.NAME_SEARCH and screen.keyboard == "name"
    screen = type_text(controller, "a")
    assert [r["name"] for r in screen.rows] == ["Alex Chen", "Avery Santos", "Sam Rivera"]
    screen = type_text(controller, "v")
    assert [r["name"] for r in screen.rows] == ["Avery Santos"]
    assert screen.rows[0]["sub"].startswith("U001")
    screen = press(controller, "1")                              # their page
    assert controller.state == kc.PERSON and screen.person["name"] == "Avery Santos"
    assert "1  Change team" in screen.lines
    screen = press(controller, "1")                              # straight to the teams
    assert controller.state == kc.PICK_TEAM and screen.view == "teams"
    assert press(controller, "X").title == "Avery Santos"       # back: their page again
    assert press(controller, "X").entry == "Av_"                 # back: the search, as it was
    press(controller, "1")
    press(controller, "1")
    screen = press(controller, "2")                              # Impact
    assert screen.title == "Team changed" and "B002" in screen.lines[1]


def test_search_by_name_from_an_id_screen(services):
    controller, users, *_ = make(services)
    users.add("Quinn Foster", "C")
    screen = press(controller, "*2468#12")                       # Enroll a card: user ID
    assert "?  Search by name" in screen.lines
    press(controller, "?")
    type_text(controller, "qu")
    screen = controller.handle_char("\n")                        # Done picks the first one
    assert controller.state == kc.ENROLL_SCAN and "Quinn Foster" in screen.lines[0]
    assert "?  Search by name" not in press(controller, "XX#").lines   # not for sign-in


def test_deactivated_people_only_found_where_it_makes_sense(services):
    controller, users, *_ = make(services)
    robin = users.add("Robin Alder", "B")
    users.set_active(robin["code"], False)
    press(controller, "*2468#12?")
    assert type_text(controller, "rob").rows == []
    press(controller, "XX6?")                                    # Deactivate / reactivate
    screen = type_text(controller, "rob")
    assert screen.rows[0]["sub"].endswith("deactivated")
    assert press(controller, "1").title == "Reactivate Robin Alder?"


def test_leaderboard_screen_and_rank_search(services, clock):
    controller, users, *_ = make(services)
    alex, sam = users.add("Alex Chen", "A"), users.add("Sam Rivera", "B")
    users.add("Jordan Lee", "C")
    controller.attendance.adjust(alex["id"], 3600)
    controller.attendance.adjust(sam["id"], 7200)
    screen = press(controller, "~")
    assert screen.view == "board" and controller.state == kc.BOARD
    assert [(r["rank"], r["name"]) for r in screen.rows] == [
        (1, "Sam Rivera"), (2, "Alex Chen"), (None, "Jordan Lee")]   # no time: no rank
    press(controller, "?")
    screen = type_text(controller, "alex")
    assert screen.rows[0]["rank"] == 2 and screen.rows[0]["right"] == "1h 00m"
    screen = controller.handle_char("\n")                       # Done: only the matches
    assert controller.state == kc.BOARD and len(screen.rows) == 1
    assert "#  Show everyone" in screen.lines[-1]
    assert len(press(controller, "#").rows) == 3
    assert press(controller, "X").view == "idle"


def test_a_card_mid_menu_is_a_popup(services, db):
    controller, users, *_ = make(services)
    users.enroll_tag("04AA", users.add("Taylor Kim", "A")["id"])
    press(controller, "*2468#1")
    screen = controller.handle_card("04AA")
    assert screen.toast and screen.title == "Taylor Kim signed in"
    assert controller.state == kc.PEOPLE_MENU                    # the menu stays
    imported = add_imported(db, "Avery Santos", 1)
    users.enroll_tag("04BB", imported)
    screen = controller.handle_card("04BB")                      # picking a team needs the screen
    assert screen.toast and "start screen" in screen.lines[0]
    assert controller.state == kc.PEOPLE_MENU
    press(controller, "XX")
    assert controller.handle_card("04BB").title == "Welcome, Avery Santos!"   # from the start


def test_cards_dont_keep_an_admin_menu_open(services):
    controller, users, *_ = make(services)
    users.enroll_tag("04AA", users.add("Taylor Kim", "A")["id"])
    now = [0.0]
    controller.monotonic = lambda: now[0]
    press(controller, "*2468#")
    now[0] = 25
    controller.handle_card("04AA")
    now[0] = 31
    assert controller.check_timeout().view == "idle"


def test_scheduled_sign_out(services, db, clock):
    controller, users, *_ = make(services)
    schedule = AutoSignOut(db, clock=clock)
    controller.schedule = schedule
    alex = users.add("Alex Chen", "A")
    sam = users.add("Sam Rivera", "B")
    clock.now = clock.now.replace(hour=15, minute=0)
    press(controller, "*2468#27#2100#")                          # 21:00, keep their hours
    press(controller, "#")
    press(controller, "#1530#B")                                 # 15:30, drop them
    assert schedule.times() == [("15:30", False), ("21:00", True)]
    assert controller.state == kc.SCHEDULE
    controller.attendance.toggle(alex["id"], "card")
    clock.advance(minutes=20)
    controller.attendance.toggle(sam["id"], "card")
    clock.advance(minutes=40)                                    # 16:00: past 15:30
    toast = controller.background_checks()
    assert toast.toast and toast.title == "Scheduled sign-out: 2 signed out"
    assert controller.attendance.user_stats(alex["id"]).total_seconds == 0   # dropped
    assert controller.background_checks() is None                # only once
    controller.attendance.toggle(alex["id"], "card")             # back in at 16:00
    clock.advance(hours=6)                                       # 22:00: an hour late
    controller.background_checks()
    stats = controller.attendance.user_stats(alex["id"])
    assert not stats.signed_in and stats.total_seconds == 5 * 3600   # counted to 21:00
    controller.attendance.toggle(sam["id"], "card")
    clock.advance(days=2, hours=13)                              # the Pi was off: no late run
    assert schedule.run_due() == [] and controller.attendance.user_stats(sam["id"]).signed_in
    press(controller, "1B")                                      # remove 15:30
    assert schedule.times() == [("21:00", True)]
    press(controller, "1#")                                      # 21:00 now drops hours
    assert schedule.times() == [("21:00", False)]


def test_schedule_times_must_be_real():
    assert parse_time("930") == "09:30" and parse_time("2100") == "21:00"
    for bad in ("2460", "2500", "12", ""):
        try:
            parse_time(bad)
        except ScheduleError:
            continue
        raise AssertionError(bad)


def test_clock_jump_moves_sessions_since_start(services, db, clock):
    controller, users, *_ = make(services)
    alex = users.add("Alex Chen", "A")
    wall, mono = [1_000_000.0], [50.0]
    guard = ClockGuard(db, wall=lambda: wall[0], monotonic=lambda: mono[0])
    guard._since = clock.now - timedelta(minutes=1)
    controller.attendance.toggle(alex["id"], "card")             # signed in on the old time
    wall[0] += 30
    mono[0] += 30
    assert guard.check() == 0                                    # time moving on normally
    wall[0] += 3 * 86400                                         # the internet sets the clock
    assert guard.check() == 3 * 86400
    with db.transaction() as cur:
        session = repo.get_open_session(cur, alex["id"])
    assert session["sign_in_at"] == clock.now + timedelta(days=3)
    clock.advance(days=3, hours=2)                               # 2 hours later, real time
    result = controller.attendance.toggle(alex["id"], "card")
    assert result.session_seconds == 2 * 3600                    # not 3 days and 2 hours


def test_sign_in_blocks(services, db, clock):
    from nfc_login.services.blocks import Block, SignInBlocks
    controller, users, *_ = make(services)
    blocks = SignInBlocks(db)
    controller.attendance.sign_in_blocks = blocks
    users.enroll_tag("04AA", users.add("Alex Chen", "A")["id"])
    clock.now = clock.now.replace(hour=8, minute=0)              # Tuesday 1 Sep 2026
    press(controller, "*2468#28#0900#1500#")                     # 09:00-15:00, Mon-Fri
    screen = press(controller, "7")                              # add Sunday
    assert "✓  Sunday" in screen.lines[7]
    press(controller, "#")
    assert blocks.list() == [Block("09:00", "15:00", "123457")]
    press(controller, "XXX")
    assert controller.handle_card("04AA").title == "Welcome, Alex Chen!"   # 08:00: open
    clock.advance(hours=2)                                       # 10:00: blocked...
    assert controller.handle_card("04AA").title.startswith("Goodbye")      # ...but out works
    clock.advance(minutes=1)
    screen = controller.handle_card("04AA")
    assert screen.title == "Signing in is closed" and "from 09:00 to 15:00" in screen.lines[0]
    assert "closed until 15:00" in controller.idle_screen().lines[0]
    press(controller, "*2468#22A001")                            # admins still can
    assert controller.state == kc.IDLE and controller.attendance.user_stats(
        users.get_by_code("A001")["id"]).signed_in
    press(controller, "XXX")
    press(controller, "*2468#22A001")                            # signed out again
    press(controller, "XXX")
    clock.advance(days=4)                                        # Saturday: no block
    assert controller.handle_card("04AA").title.startswith("Welcome")


def test_overnight_block():
    from datetime import datetime
    from nfc_login.services.blocks import Block
    night = Block("22:00", "06:00", "5")                         # Friday night
    assert night.covers(datetime(2026, 9, 4, 23, 0))             # Fri 23:00
    assert night.covers(datetime(2026, 9, 5, 5, 59))             # Sat 05:59
    assert not night.covers(datetime(2026, 9, 5, 6, 0))
    assert not night.covers(datetime(2026, 9, 5, 23, 0))         # Saturday night isn't on


def test_delete_someone_from_their_page(services, db, clock):
    controller, users, *_ = make(services)
    jo = users.add("Jo Park", "C")
    users.add("Kim Lee", "C")
    users.enroll_tag("04JO", jo["id"])
    controller.handle_card("04JO")                               # signed in, then out
    clock.advance(hours=2)
    controller.handle_card("04JO")
    controller.attendance.adjust(jo["id"], 600, "test")
    press(controller, "*2468#18")
    type_text(controller, "jo")
    screen = press(controller, "1")
    assert "8  Delete" in screen.lines
    screen = press(controller, "8")
    assert screen.title == "Delete Jo Park for good?" and screen.tone == "error"
    assert screen.view == "dialog"
    assert press(controller, "X").title == "Jo Park"            # cancel: their page again
    press(controller, "8")
    screen = press(controller, "#")
    assert screen.title == "Jo Park deleted"
    with db.transaction() as cur:
        assert repo.get_user(cur, jo["id"]) is None
        for table in ("tags", "sessions", "adjustments"):
            cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE user_id = %s", (jo["id"],))
            assert cur.fetchone()["n"] == 0
    assert users.get_by_code("C002")["username"] == "Kim Lee"   # others untouched
    clock.advance(minutes=1)
    assert controller.handle_card("04JO").title != "Welcome, Jo Park!"   # card is free
    assert users.add("New Person", "C")["code"] == "C001"   # the freed ID is reused


def test_delete_works_on_a_deactivated_person(services):
    controller, users, *_ = make(services)
    users.add("Robin Alder", "B")
    users.set_active("B001", False)
    press(controller, "*2468#18")
    type_text(controller, "rob")
    press(controller, "1")
    press(controller, "8")
    assert press(controller, "#").title == "Robin Alder deleted"
    assert users.list(include_inactive=True) == []


def test_cli_delete_asks_to_type_delete(services, monkeypatch, capsys):
    from argparse import Namespace

    from nfc_login.admin import cli
    attendance, seasons, users = services
    users.add("Drew Hale", "A")
    s = Namespace(users=users)
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    cli.cmd_user_delete(s, Namespace(code="A001", yes=False), None)
    assert users.get_by_code("A001")
    monkeypatch.setattr("builtins.input", lambda _prompt: "DELETE")
    cli.cmd_user_delete(s, Namespace(code="A001", yes=False), None)
    assert users.list(include_inactive=True) == []
