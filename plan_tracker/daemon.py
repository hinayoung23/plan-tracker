"""Plan Tracker Daemon — standalone background process.

Runs the reminder engine independently of any MCP client.
Writes notifications to the shared notification queue file.

Usage:
  python daemon.py          # start in foreground
  python daemon.py --daemon # start as background daemon
"""

import argparse
import fcntl
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

logger = logging.getLogger("plan_tracker.daemon")
_PID_LOCK_PROTOCOL = "lock-v1"
_LOGGING_CONFIGURED = False


def _configure_logging() -> None:
    """Configure daemon-only logging after background forking is complete."""
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)
    os.umask(0o077)
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
    try:
        os.chmod(LOG_FILE, 0o600)
    except OSError:
        pass
    _LOGGING_CONFIGURED = True


def write_pid() -> int:
    """Write PID and hold an exclusive lock on the PID file."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(PID_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        os.fchmod(fd, 0o600)
        payload = f"{os.getpid()}:{int(time.time())}:{_PID_LOCK_PROTOCOL}"
        data = payload.encode()
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        written = 0
        while written < len(data):
            n = os.write(fd, data[written:])
            if n <= 0:
                raise OSError(f"write returned {n}")
            written += n
        os.fsync(fd)
    except Exception:
        os.close(fd)
        raise
    return fd


def _read_pid_record() -> tuple[int | None, str]:
    try:
        parts = PID_FILE.read_text().strip().split(":")
        pid = int(parts[0])
        protocol = parts[2] if len(parts) >= 3 else "legacy"
        return pid, protocol
    except (ValueError, OSError, IndexError):
        return None, ""


def read_pid() -> int | None:
    """Read the daemon PID from the PID file.  Returns None if not found."""
    return _read_pid_record()[0]


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
        # Lock acquired → no lock-protocol daemon holds it.  A daemon from a
        # pre-lock release may still be alive; recognize only the legacy PID
        # record so stale lock-v1 PIDs cannot cause permanent false positives.
        legacy_pid, protocol = _read_pid_record()
        fcntl.flock(fd, fcntl.LOCK_UN)
        if protocol == "legacy" and legacy_pid:
            try:
                os.kill(legacy_pid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
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
    _configure_logging()
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
    _configure_logging()
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
