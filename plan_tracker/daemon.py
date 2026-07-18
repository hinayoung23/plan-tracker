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
    """Write the current PID and start time to the PID file."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(f"{os.getpid()}:{int(time.time())}")


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


def _verify_daemon_process(pid: int) -> bool:
    """Check that the process at *pid* is actually a plan-tracker daemon.

    Prevents false positives when a stale PID is reused by an unrelated
    process (the root cause of dual-daemon zombies).
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        return "plan_tracker.daemon" in result.stdout
    except Exception:
        return False


def is_running() -> bool:
    """Check if a daemon process is currently running.

    Verifies both that the PID exists AND that it belongs to a
    plan-tracker daemon (not a PID-reuse collision).
    """
    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return _verify_daemon_process(pid)
    except (ProcessLookupError, PermissionError):
        remove_pid()
        return False


def _kill_any_daemon() -> int:
    """Kill every plan-tracker daemon process on the system.

    Scans for *all* daemon.py processes (not just the one in the
    PID file) so that zombie daemons left by PID-file races are
    cleaned up.  Returns the number of processes killed.
    """
    killed = 0
    try:
        result = subprocess.run(
            ["pgrep", "-f", "plan_tracker.daemon"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            pid_str = line.strip()
            if not pid_str:
                continue
            try:
                pid = int(pid_str)
                if pid == os.getpid():
                    continue  # Don't kill ourselves
                os.kill(pid, signal.SIGTERM)
                killed += 1
                logger.info("Killed old daemon (PID: %d)", pid)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
    except Exception:
        pass

    # Wait for killed processes to exit
    if killed > 0:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(
                    ["pgrep", "-f", "plan_tracker.daemon"],
                    capture_output=True, text=True, timeout=5,
                )
                remaining = [p for p in result.stdout.strip().splitlines()
                           if p.strip() and int(p.strip()) != os.getpid()]
                if not remaining:
                    break
            except Exception:
                break
            time.sleep(0.5)

    remove_pid()
    return killed


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
    # Kill any existing daemon first — prevents dual-daemon zombies
    _kill_any_daemon()

    write_pid()
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
        remove_pid()
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
            remove_pid()
            logger.info("Plan Tracker daemon stopped")


def run_daemon() -> None:
    """Fork to background and run."""
    # Kill any existing daemon first — prevents dual-daemon zombies
    _kill_any_daemon()

    daemonize()
    write_pid()
    logger.info("Plan Tracker daemon started in background (PID: %d)", os.getpid())

    engine = ReminderEngine()
    _shutting_down = False

    def _shutdown(signum, frame):
        nonlocal _shutting_down
        if _shutting_down:
            return
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
