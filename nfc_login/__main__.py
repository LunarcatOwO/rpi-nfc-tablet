# rpi-nfc-tablet: touch NFC sign-in kiosk for a Raspberry Pi 4 B
# Created by LunarcatOwO (https://github.com/LunarcatOwO)
# Copyright (C) 2026 LunarcatOwO
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. It comes WITHOUT ANY WARRANTY; see the LICENSE file.

"""Start the kiosk:  python3 -m nfc_login [--config config.toml] [--simulate] [--web-ui [PORT]]"""

# The screen is a local web page (nfc_login/ui/kiosk.html) that the Electron
# app shows fullscreen. Run by hand, open http://127.0.0.1:8081/ in a browser.

from __future__ import annotations

import argparse
import logging
import os
import sys

from nfc_login.app import build_services
from nfc_login.config import load_config
from nfc_login.hardware import create_reader
from nfc_login.kiosk.controller import KioskController
from nfc_login.kiosk.nfc_worker import NfcWorker
from nfc_login.services.blocks import SignInBlocks
from nfc_login.services.clockguard import ClockGuard
from nfc_login.services.schedule import AutoSignOut
from nfc_login.services.system import SystemActions
from nfc_login.services.updates import UpdateChecker
from nfc_login.web.server import start_in_background

DEFAULT_PORT = 8081


def main() -> None:
    """Read the command line, build the services and run the kiosk until it stops."""
    parser = argparse.ArgumentParser(description="NFC sign-in tablet kiosk")
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--simulate", action="store_true",
                        help="run without the NFC reader (tap pretend cards on screen)")
    parser.add_argument("--web-ui", type=int, nargs="?", const=DEFAULT_PORT, metavar="PORT",
                        help=f"port for the kiosk screen (default {DEFAULT_PORT})")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(args.config)
    if args.simulate:  # the command line wins over config.toml
        config.hardware["mode"] = "simulated"

    services = build_services(config)
    # There must always be an active season to credit hours to.
    season = services.seasons.ensure_active()
    logging.info("active season: %s", season["name"])

    reader = create_reader(config)  # the PN532, or a stand-in in simulated mode
    logging.info("NFC reader: %s", reader.firmware_version())

    # The admin menu can always check for updates by hand; with [updates]
    # enabled it also checks by itself every few hours.
    updates = UpdateChecker(check_hours=config.updates["check_hours"], config_path=args.config)
    if config.updates["enabled"]:
        updates.start()

    services.attendance.sign_in_blocks = SignInBlocks(services.db)   # Hours > Sign-in blocks
    controller = KioskController(
        services.attendance,
        services.users,
        reader=reader,
        site_url=config.tag["site_url"],
        write_tags=config.hardware["nfc"]["write_tags"],
        result_seconds=config.ui["result_seconds"],
        idle_timeout=config.ui["idle_timeout_seconds"],
        seasons=services.seasons,
        system=SystemActions(),
        updates=updates,
        reopen_hint="To start it again, restart the Pi, or open NFC Kiosk from the desktop menu.",
        schedule=AutoSignOut(services.db, config.attendance["max_session_hours"]),
        clock_guard=ClockGuard(services.db),
    )

    # Live "who's here" page on port 8080, in its own thread.
    if not start_in_background(services, config):
        logging.info("live page: off")
    simulated = reader if config.hardware["mode"] == "simulated" else None
    run_web_ui(args.web_ui or DEFAULT_PORT, controller, config, reader, simulated, updates)


def restart() -> None:
    """Admin menu restart (or an installed update): start again with the code now on disk."""
    logging.info("restarting the kiosk")
    # execv replaces this process, so no second copy fights over the reader.
    os.execv(sys.executable, [sys.executable, "-m", "nfc_login", *sys.argv[1:]])


def run_web_ui(port, controller, config, reader, simulated, updates) -> None:
    """Serve the kiosk screen on 127.0.0.1:port and poll the reader until it stops."""
    from nfc_login.ui.web_kiosk import WebKiosk, serve
    kiosk = WebKiosk(controller, config.ui, simulated_reader=simulated, updates=updates)
    server = serve(kiosk, port)
    kiosk.on_restart = server.shutdown  # a restart stops serve_forever() below
    worker = NfcWorker(reader, controller, kiosk.publish)
    controller.reader_status = worker.status   # System info: how the reader is doing
    worker.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    if kiosk.restart_requested:
        # Started without the app's control pipe: restart just this program.
        restart()


if __name__ == "__main__":
    main()
