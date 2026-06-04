"""Plan Tracker CLI — command-line management tool.

Usage:
  python cli.py daemon start       Start the daemon in background
  python cli.py daemon stop        Stop the daemon
  python cli.py daemon status      Check if daemon is running
  python cli.py notifications      Print pending notifications (for cron/agent)
  python cli.py notifications --json   Print as JSON
"""

import argparse
import json
import os
import signal
import sys
from pathlib import Path

from plan_tracker.daemon import is_running, read_pid, remove_pid, PID_FILE
from plan_tracker.notification_queue import fetch_all, get_pending_text, mark_delivered


def cmd_daemon_start() -> None:
    """Start the daemon in background."""
    if is_running():
        print(f"Daemon is already running (PID: {read_pid()})")
        return

    import subprocess
    daemon_script = Path(__file__).resolve().parent / "daemon.py"
    proc = subprocess.Popen(
        [sys.executable, str(daemon_script), "--daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    # Give it a moment to start
    proc.wait(timeout=3)
    if is_running():
        print(f"Daemon started (PID: {read_pid()})")
    else:
        print("Daemon failed to start. Check logs at data/daemon.log")


def cmd_daemon_stop() -> None:
    """Stop a running daemon."""
    pid = read_pid()
    if pid is None:
        print("Daemon is not running (no PID file)")
        return

    if not is_running():
        print(f"Stale PID file found (PID: {pid}). Cleaning up.")
        remove_pid()
        return

    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent stop signal to daemon (PID: {pid})")
    except ProcessLookupError:
        print(f"Process {pid} not found. Cleaning up PID file.")
        remove_pid()


def cmd_daemon_status() -> None:
    """Check daemon status."""
    if is_running():
        print(f"Daemon is running (PID: {read_pid()})")
    else:
        pid = read_pid()
        if pid:
            print(f"Daemon is not running (stale PID: {pid})")
        else:
            print("Daemon is not running")


def cmd_notifications(json_output: bool = False) -> None:
    """Print pending notifications."""
    if json_output:
        pending = fetch_all()
        print(json.dumps(pending, ensure_ascii=False, indent=2))
    else:
        text = get_pending_text()
        if text:
            print(text, end="")
        # If empty, print nothing (allows "NO_REPLY" detection in cron)


def cmd_ack(ids: list[str]) -> None:
    """Mark notifications as delivered."""
    count = mark_delivered(ids)
    print(f"Marked {count} notification(s) as delivered")


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan Tracker CLI")
    sub = parser.add_subparsers(dest="command", help="Commands")

    # daemon subcommands
    daemon_parser = sub.add_parser("daemon", help="Daemon management")
    daemon_parser.add_argument("action", choices=["start", "stop", "status"])

    # notifications
    notif_parser = sub.add_parser("notifications", help="Show pending notifications")
    notif_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # ack
    ack_parser = sub.add_parser("ack", help="Mark notifications as delivered")
    ack_parser.add_argument("ids", nargs="+", help="Notification IDs to ack")

    args = parser.parse_args()

    if args.command == "daemon":
        if args.action == "start":
            cmd_daemon_start()
        elif args.action == "stop":
            cmd_daemon_stop()
        elif args.action == "status":
            cmd_daemon_status()
    elif args.command == "notifications":
        cmd_notifications(json_output=args.json)
    elif args.command == "ack":
        cmd_ack(args.ids)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
