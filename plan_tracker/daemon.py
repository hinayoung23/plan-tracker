"""Plan Tracker Daemon — standalone background process.

Runs the reminder engine independently of any MCP client.
Writes notifications to the shared notification queue file.

Usage:
  python daemon.py          # start in foreground
  python daemon.py --daemon # start as background daemon
"""

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path

from plan_tracker.reminder import ReminderEngine
from plan_tracker.storage import DATA_DIR

PID_FILE = DATA_DIR / "daemon.pid"
LOG_FILE = DATA_DIR / "daemon.log"

# Rotate after ~1 MB, keep 3 backups (~4 MB total max)
_LOG_MAX_BYTES = 1_048_576
_LOG_BACKUP_COUNT = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        ),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("plan_tracker.daemon")


def write_pid() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def read_pid() -> int | None:
    try:
        if PID_FILE.exists():
            return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        pass
    return None


def remove_pid() -> None:
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except OSError:
        pass


def is_running() -> bool:
    """Check if a daemon process is currently running."""
    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        remove_pid()
        return False


def daemonize() -> None:
    """Fork the process into the background."""
    # First fork
    if os.fork() > 0:
        sys.exit(0)

    # Detach from terminal
    os.setsid()

    # Second fork
    if os.fork() > 0:
        sys.exit(0)

    # Redirect stdio
    sys.stdin = open(os.devnull, "r")
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")


def run_foreground() -> None:
    """Run the daemon in the foreground (for debugging / launchd)."""
    if is_running():
        logger.error("Daemon is already running (PID: %d)", read_pid())
        sys.exit(1)

    write_pid()
    logger.info("Plan Tracker daemon started (PID: %d)", os.getpid())

    engine = ReminderEngine()
    _shutting_down = False

    def _shutdown(signum, frame):
        nonlocal _shutting_down
        if _shutting_down:
            return  # Already shutting down — avoid double-stop
        _shutting_down = True
        logger.info("Received signal %d, shutting down...", signum)
        engine.stop()
        remove_pid()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    engine.start()

    try:
        # Keep the main thread alive while the engine thread runs
        while engine._thread and engine._thread.is_alive():
            engine._thread.join(timeout=10)
    except KeyboardInterrupt:
        pass
    finally:
        if not _shutting_down:
            engine.stop()
            remove_pid()
            logger.info("Plan Tracker daemon stopped")


def run_daemon() -> None:
    """Fork to background and run."""
    if is_running():
        logger.error("Daemon is already running (PID: %d)", read_pid())
        sys.exit(1)

    daemonize()
    write_pid()
    logger.info("Plan Tracker daemon started in background (PID: %d)", os.getpid())

    engine = ReminderEngine()
    _shutting_down = False

    def _shutdown(signum, frame):
        nonlocal _shutting_down
        if _shutting_down:
            return  # Already shutting down — avoid double-stop
        _shutting_down = True
        engine.stop()
        remove_pid()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)

    engine.start()

    try:
        while engine._thread and engine._thread.is_alive():
            engine._thread.join(timeout=10)
    except KeyboardInterrupt:
        pass
    finally:
        if not _shutting_down:
            engine.stop()
            remove_pid()


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan Tracker Daemon")
    parser.add_argument(
        "--daemon", action="store_true",
        help="Run as background daemon (fork to background)",
    )
    args = parser.parse_args()

    if args.daemon:
        run_daemon()
    else:
        run_foreground()


if __name__ == "__main__":
    main()
