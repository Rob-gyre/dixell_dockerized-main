#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
supervisor.py — Long-running scheduler for the collector/emitter.

Runs the target script once, sleeps for the interval, repeats.
Handles SIGTERM so `docker stop` terminates cleanly.
Streams the child's output live to stdout (captured by Docker's json-file log driver).

Usage:
    python supervisor.py <script>.py <interval_seconds>

Example:
    python supervisor.py collector_shaprepoint.py 900
    python supervisor.py emitter.py 300
"""

import os
import sys
import time
import signal
import subprocess
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s supervisor - %(message)s",
    stream=sys.stdout,
)
LOG = logging.getLogger(__name__)

_shutdown = False
_current_proc = None


def _handle_term(signum, frame):
    global _shutdown
    LOG.info("Received signal %d, initiating shutdown", signum)
    _shutdown = True
    if _current_proc and _current_proc.poll() is None:
        LOG.info("Forwarding signal to child PID %d", _current_proc.pid)
        try:
            _current_proc.terminate()
        except Exception as e:
            LOG.error("Failed to terminate child: %s", e)


signal.signal(signal.SIGTERM, _handle_term)
signal.signal(signal.SIGINT, _handle_term)


def run_once(script_path: str) -> int:
    global _current_proc
    started = time.monotonic()
    LOG.info("Starting run: %s", script_path)
    try:
        _current_proc = subprocess.Popen(
            [sys.executable, script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
        )
        assert _current_proc.stdout is not None
        for line in _current_proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
        rc = _current_proc.wait()
    except Exception as e:
        LOG.error("Exception running %s: %s", script_path, e)
        rc = 127
    finally:
        _current_proc = None

    elapsed = time.monotonic() - started
    LOG.info("Finished run: %s rc=%d elapsed=%.2fs", script_path, rc, elapsed)
    return rc


def sleep_interruptible(seconds: int) -> None:
    end = time.monotonic() + seconds
    while not _shutdown and time.monotonic() < end:
        time.sleep(1)


def main() -> int:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <script.py> <interval_seconds>", file=sys.stderr)
        return 2

    script = sys.argv[1]
    try:
        interval = int(sys.argv[2])
    except ValueError:
        LOG.error("Invalid interval: %s", sys.argv[2])
        return 2

    if not os.path.exists(script):
        LOG.error("Script not found: %s", script)
        return 2
    if interval < 10:
        LOG.error("Interval must be >= 10 seconds, got %d", interval)
        return 2

    LOG.info("Supervisor started: script=%s interval=%ds pid=%d",
             script, interval, os.getpid())

    while not _shutdown:
        run_once(script)
        if _shutdown:
            break
        LOG.info("Sleeping %ds until next run", interval)
        sleep_interruptible(interval)

    LOG.info("Supervisor exited cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
