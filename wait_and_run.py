#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wait_and_run.py — Wait for a serial device to appear, then start the supervisor.

This solves the "Pi powered on with the controller unplugged" problem:
instead of the container failing to start (which is what a hard `devices:`
mapping does when the device is absent), the container starts, waits
patiently for the device, logs its status, and proceeds the moment the
device shows up. If the device never appears it keeps waiting — no crash,
no manual intervention needed when someone finally plugs the controller in.

Usage:
    python wait_and_run.py <device_path> <script.py> <interval_seconds>

Example:
    python wait_and_run.py /dev/ttyACM0 collector_shaprepoint.py 900
"""

import os
import sys
import time
import signal
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s wait_and_run - %(message)s",
    stream=sys.stdout,
)
LOG = logging.getLogger(__name__)

_shutdown = False


def _handle_term(signum, frame):
    global _shutdown
    LOG.info("Received signal %d while waiting for device, exiting", signum)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_term)
signal.signal(signal.SIGINT, _handle_term)


def main() -> int:
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <device_path> <script.py> <interval_seconds>",
              file=sys.stderr)
        return 2

    device = sys.argv[1]
    script = sys.argv[2]
    interval = sys.argv[3]

    LOG.info("Waiting for device %s ...", device)

    waited = 0
    while not _shutdown:
        if os.path.exists(device):
            LOG.info("Device %s present after %ds — starting supervisor", device, waited)
            # Replace this process with the supervisor (PID stays 1 under tini)
            os.execv(sys.executable,
                     [sys.executable, "supervisor.py", script, interval])
        if waited % 15 == 0:
            LOG.info("Still waiting for %s (%ds elapsed). "
                     "Is the controller plugged in?", device, waited)
        time.sleep(1)
        waited += 1

    LOG.info("Shutdown requested before device appeared — exiting cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
