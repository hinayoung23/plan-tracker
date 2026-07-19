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
import subprocess
import sys
import time
from pathlib import Path

from plan_tracker.reminder import ReminderEngine
from plan_tracker.storage import DATA_DIR

PID_FILE = DATA_DIR / "daemon.pid"
LOG_FILE = DATA_DIR / "daemon.log"

# Ensure the data directory exists before configuring logging.
# When installed as a wheel, DATA_DIR may not exist yet and the
# RotatingFileHandler would raise FileNotFoundError on import.
DATA_DIR.mkdir(parents=True, exist_ok=True)

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


def write_pid() -> int:
    """Write PID and hold an exclusive lock on the PID file.

    The lock is held for the lifetime of the daemon.  is_running()
    detects it via non-blocking lock acquisition.  Returns the open
    file descriptor (caller must close on shutdown).
    """
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(PID_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    payload = f"{os.getpid()}:{int(time.time())}"
    os.write(fd, payload.encode())
    return fd


def read_pid() -> int | None:
    """Read the daemon PID from the PID file.  Returns None if not found."""
    try:
        if PID_FILE.exists():
            raw = PID_FILE.read_text().strip()
            return int(raw.split(":")[0])
    except (ValueError, OSError):
        pass
    return None


def remove_pid() -> None:
    """Remove the PID file."""
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except OSError:
        pass


def is_running() -> bool:
    """Check if a daemon process holds the PID file lock.

    The daemon holds fcntl.LOCK_EX on daemon.pid while running.
    If we can acquire LOCK_EX|LOCK_NB, no daemon is running.
    This works in sandboxes where ps/pgrep are unavailable.
    """
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(PID_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return True  # Can't open — assume running to be safe
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Lock acquired → no other daemon holds it
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except (BlockingIOError, OSError):
        # Lock held → daemon is running
        return True
    finally:
        os.close(fd)


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
        logger.error("Daemon is already running")
        sys.exit(1)

    pid_fd = write_pid()
    logger.info("Plan Tracker daemon started (PID: %d)", os.getpid())

    engine = ReminderEngine()
    _shutting_down = False

    def _shutdown(signum, frame):
        nonlocal _shutting_down
        if _shutting_down:
            return
        _shutting_down = True
        logger.info("Received signal %d, shutting down...", signum)
        engine.stop()
        os.close(pid_fd)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    engine.start()

    try:
        while engine._thread and engine._thread.is_alive():
            engine._thread.join(timeout=10)
    except KeyboardInterrupt:
        pass
    finally:
        if not _shutting_down:
            engine.stop()
            os.close(pid_fd)
            logger.info("Plan Tracker daemon stopped")


def run_daemon() -> None:
    """Fork to background and run."""
    if is_running():
        logger.error("Daemon is already running")
        sys.exit(1)

    daemonize()
    pid_fd = write_pid()
    logger.info("Plan Tracker daemon started in background (PID: %d)", os.getpid())

    engine = ReminderEngine()
    _shutting_down = False

    def _shutdown(signum, frame):
        nonlocal _shutting_down
        if _shutting_down:
            return
        _shutting_down = True
        engine.stop()
        os.close(pid_fd)
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
            os.close(pid_fd)


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
