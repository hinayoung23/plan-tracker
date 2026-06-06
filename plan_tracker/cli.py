"""Plan Tracker CLI — command-line management tool.

Usage:
  python cli.py daemon start       Start the daemon in background
  python cli.py daemon stop        Stop the daemon
  python cli.py daemon status      Check if daemon is running
  python cli.py notifications      Print pending notifications (for cron/agent)
  python cli.py notifications --json   Print as JSON
  python cli.py cron-setup         Install/update the OpenClaw cron job
"""

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from plan_tracker.daemon import is_running, read_pid, remove_pid, PID_FILE
from plan_tracker.notification_queue import fetch_all, get_pending_text, mark_delivered

# Default cron job configuration
_DEFAULT_CRON_JOB_ID = "plan-tracker-notification-check"
_DEFAULT_CRON_INTERVAL_MS = 300000  # 5 minutes
_DEFAULT_OPENCLAW_CRON_DIR = Path.home() / ".openclaw" / "cron"
_DEFAULT_CRON_FILE = _DEFAULT_OPENCLAW_CRON_DIR / "jobs.json"


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


def cmd_notifications(json_output: bool = False, ack: bool = False) -> None:
    """Print pending notifications.

    When --ack is set, fetched notifications are immediately marked as
    delivered so they are never sent twice.  Use this from cron / polling
    loops that forward the output to the user.

    Also auto-starts the daemon if it is not running, so that the
    cron-driven poll is self-healing.
    """
    # Auto-start daemon if needed — self-healing for cron-driven polling
    if not is_running():
        cmd_daemon_start()

    pending = fetch_all()
    if json_output:
        print(json.dumps(pending, ensure_ascii=False, indent=2))
    else:
        text = get_pending_text()
        if text:
            print(text, end="")
        # If empty, print nothing (allows "NO_REPLY" detection in cron)

    if ack and pending:
        ids = [n["id"] for n in pending]
        count = mark_delivered(ids)
        print(f"Auto-acked {count} notification(s)", file=sys.stderr)


def cmd_ack(ids: list[str]) -> None:
    """Mark notifications as delivered."""
    count = mark_delivered(ids)
    print(f"Marked {count} notification(s) as delivered")


def _validate_qq_id(qq_id: str) -> str | None:
    """Return an error message if the QQ ID looks invalid, or None if ok."""
    if not qq_id or not qq_id.strip():
        return "QQ ID must not be empty"
    stripped = qq_id.strip()
    # QQ IDs are hexadecimal strings, typically 32 chars for UIN hash
    if not all(c in "0123456789ABCDEFabcdef" for c in stripped):
        return f"QQ ID '{stripped}' contains non-hex characters — please double-check"
    if len(stripped) < 4:
        return f"QQ ID '{stripped}' seems too short — please double-check"
    return None


def cmd_cron_setup(
    qq_id: str = "",
    interval_minutes: int = 5,
    dry_run: bool = False,
    job_id: str = _DEFAULT_CRON_JOB_ID,
) -> None:
    """Install or update the OpenClaw cron job for notification polling.

    Generates a correct createdAtMs timestamp automatically, validates
    parameters, and merges the job into ~/.openclaw/cron/jobs.json.

    Parameters
    ----------
    qq_id:
        The QQ ID (hex string) to deliver notifications to.
        Maps to ``qqbot:c2c:<qq_id>`` in the delivery config.
    interval_minutes:
        Polling interval in minutes (default 5, minimum 1).
    dry_run:
        If True, print the job JSON to stdout instead of writing it.
    job_id:
        Cron job identifier (default: plan-tracker-notification-check).
    """
    # ── Validate parameters ──────────────────────────────────────────
    errors: list[str] = []

    err = _validate_qq_id(qq_id)
    if err:
        errors.append(err)

    if interval_minutes < 1:
        errors.append(f"Interval must be at least 1 minute (got {interval_minutes})")
    if interval_minutes > 1440:
        errors.append(f"Interval should not exceed 1440 minutes / 1 day (got {interval_minutes})")

    if errors:
        print("Error(s):", file=sys.stderr)
        for e in errors:
            print(f"  • {e}", file=sys.stderr)
        sys.exit(1)

    # ── Build the job (timestamp auto-generated) ─────────────────────
    now_ms = int(time.time() * 1000)
    interval_ms = interval_minutes * 60 * 1000

    job = {
        "id": job_id,
        "name": "plan-tracker Notification Check",
        "description": (
            f"Poll plan-tracker notification queue every {interval_minutes} min "
            "and forward to QQ"
        ),
        "enabled": True,
        "createdAtMs": now_ms,
        "schedule": {
            "kind": "every",
            "everyMs": interval_ms,
            "anchorMs": now_ms,
        },
        "sessionTarget": "isolated",
        "wakeMode": "now",
        "payload": {
            "kind": "agentTurn",
            "message": (
                f"exec {sys.executable}"
                " -m plan_tracker.cli notifications --ack。"
                "如果输出为空则回复NO_REPLY，否则原样发送给用户（不要加多余内容）。"
            ),
            "lightContext": True,
            "timeoutSeconds": 30,
        },
        "delivery": {
            "mode": "announce",
            "channel": "qqbot",
            "to": f"qqbot:c2c:{qq_id.strip()}",
            "accountId": "default",
        },
        "state": {},
    }

    # ── Dry-run: print and exit ──────────────────────────────────────
    if dry_run:
        print(json.dumps(job, ensure_ascii=False, indent=2))
        print("\n# Dry-run mode — not written to disk.", file=sys.stderr)
        print(f"# Timestamp validated: {now_ms} ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(now_ms / 1000))})", file=sys.stderr)
        return

    # ── Read existing jobs ───────────────────────────────────────────
    _DEFAULT_OPENCLAW_CRON_DIR.mkdir(parents=True, exist_ok=True)

    cron_data: dict = {"version": 1, "jobs": []}
    if _DEFAULT_CRON_FILE.exists():
        try:
            with open(_DEFAULT_CRON_FILE, "r", encoding="utf-8") as fh:
                cron_data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Warning: could not read {_DEFAULT_CRON_FILE}: {exc}", file=sys.stderr)
            print("Starting with a fresh jobs list.", file=sys.stderr)
            cron_data = {"version": 1, "jobs": []}

    if "jobs" not in cron_data or not isinstance(cron_data["jobs"], list):
        cron_data["jobs"] = []

    # ── Merge: update existing or append ─────────────────────────────
    replaced = False
    for idx, existing in enumerate(cron_data["jobs"]):
        if existing.get("id") == job_id:
            cron_data["jobs"][idx] = job
            replaced = True
            break

    if not replaced:
        cron_data["jobs"].append(job)

    # ── Write ────────────────────────────────────────────────────────
    try:
        # Write to temp file first, then rename (atomic on same FS)
        tmp_path = _DEFAULT_CRON_FILE.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(cron_data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        tmp_path.replace(_DEFAULT_CRON_FILE)
    except OSError as exc:
        print(f"Error: failed to write {_DEFAULT_CRON_FILE}: {exc}", file=sys.stderr)
        sys.exit(1)

    action = "Updated" if replaced else "Created"
    print(f"{action} cron job '{job_id}' in {_DEFAULT_CRON_FILE}")
    print(f"  Interval: every {interval_minutes} min")
    print(f"  Delivery: qqbot → qqbot:c2c:{qq_id.strip()}")
    print(f"  Timestamp: {now_ms} ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(now_ms / 1000))})")
    print(f"\nRestart OpenClaw for the new job to take effect:")
    print(f"  launchctl unload ~/Library/LaunchAgents/ai.openclaw.gateway.plist")
    print(f"  launchctl load ~/Library/LaunchAgents/ai.openclaw.gateway.plist")


def _detect_plan_tracker_path() -> str:
    """Return the directory where plan_tracker is installed (for PYTHONPATH)."""
    this_dir = Path(__file__).resolve().parent  # plan_tracker/
    # If installed in editable mode, the parent is the project root
    parent = this_dir.parent
    if (parent / "pyproject.toml").exists():
        return str(parent)
    # Otherwise, find it via the package location
    return str(this_dir.parent)


def _openclaw_config_path() -> Path | None:
    """Return the path to openclaw.json if OpenClaw is installed."""
    candidate = Path.home() / ".openclaw" / "openclaw.json"
    if candidate.exists():
        return candidate
    return None


def _add_mcp_server_to_config(config_path: Path, dry_run: bool = False) -> bool:
    """Add plan-tracker MCP server to an OpenClaw config file.

    Returns True if the config was changed, False if it was already present.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Error: cannot read {config_path}: {exc}", file=sys.stderr)
        return False

    mcp = cfg.setdefault("mcp", {})
    servers = mcp.setdefault("servers", {})

    if "plan-tracker" in servers:
        print("MCP server 'plan-tracker' already configured — skipping.")
        return False

    python = sys.executable
    pkg_path = _detect_plan_tracker_path()

    servers["plan-tracker"] = {
        "command": python,
        "args": ["-m", "plan_tracker.server"],
        "env": {"PYTHONPATH": pkg_path},
    }

    if dry_run:
        print(f"\nWould add to {config_path}:")
        print(json.dumps({"mcp": {"servers": {"plan-tracker": servers["plan-tracker"]}}},
                         ensure_ascii=False, indent=2))
        return True

    # Atomic write
    tmp_path = config_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp_path.replace(config_path)
    print(f"✓ Added plan-tracker MCP server to {config_path}")
    return True


def cmd_setup(
    qq_id: str = "",
    interval_minutes: int = 5,
    dry_run: bool = False,
) -> None:
    """One-command setup: MCP config + cron job + daemon.

    Detects your environment and performs all post-install steps:
    1. Adds plan-tracker to OpenClaw's MCP server list
    2. Installs the cron job for QQ notification polling
    3. Starts the daemon
    """
    print("plan-tracker setup")
    print("=" * 40)

    # ── Step 1: MCP server config ──────────────────────────────────
    config_path = _openclaw_config_path()
    if config_path:
        print(f"\n[1/3] Configuring MCP server in {config_path}...")
        _add_mcp_server_to_config(config_path, dry_run=dry_run)
    else:
        print("\n[1/3] OpenClaw config not found at ~/.openclaw/openclaw.json")
        print("       Skipping MCP server registration.")
        print(f"       To configure manually, add to your MCP config:")
        print(f"         command: {sys.executable}")
        print(f"         args: ['-m', 'plan_tracker.server']")

    # ── Step 2: Cron job ───────────────────────────────────────────
    print(f"\n[2/3] Installing cron job...")
    if qq_id:
        cmd_cron_setup(
            qq_id=qq_id,
            interval_minutes=interval_minutes,
            dry_run=dry_run,
        )
    else:
        print("       No --qq-id provided — skipping cron job.")
        print("       Run later: python -m plan_tracker.cli cron-setup --qq-id <your-id>")

    # ── Step 3: Daemon ─────────────────────────────────────────────
    print(f"\n[3/3] Ensuring daemon is running...")
    if dry_run:
        print("       (dry-run — skipping daemon start)")
    elif is_running():
        print(f"       Daemon already running (PID: {read_pid()})")
    else:
        cmd_daemon_start()

    # ── Summary ────────────────────────────────────────────────────
    print(f"\n{'─' * 40}")
    print("Setup complete!")
    if config_path:
        print(f"  • MCP server registered in {config_path}")
    if qq_id:
        print(f"  • Cron job installed (every {interval_minutes} min → QQ)")
    print(f"  • Daemon: {'running' if is_running() else 'pending (will auto-start on first MCP call)'}")
    print(f"\n  Restart OpenClaw to apply all changes:")
    print(f"    launchctl unload ~/Library/LaunchAgents/ai.openclaw.gateway.plist")
    print(f"    launchctl load ~/Library/LaunchAgents/ai.openclaw.gateway.plist")


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan Tracker CLI")
    sub = parser.add_subparsers(dest="command", help="Commands")

    # daemon subcommands
    daemon_parser = sub.add_parser("daemon", help="Daemon management")
    daemon_parser.add_argument("action", choices=["start", "stop", "status"])

    # notifications
    notif_parser = sub.add_parser("notifications", help="Show pending notifications")
    notif_parser.add_argument("--json", action="store_true", help="Output as JSON")
    notif_parser.add_argument("--ack", action="store_true", help="Mark notifications as delivered after fetching")

    # ack
    ack_parser = sub.add_parser("ack", help="Mark notifications as delivered")
    ack_parser.add_argument("ids", nargs="+", help="Notification IDs to ack")

    # setup (one-command install)
    setup_parser = sub.add_parser(
        "setup",
        help="One-command setup: MCP config + cron job + daemon",
    )
    setup_parser.add_argument(
        "--qq-id",
        help="QQ ID (hex string) for QQBot notification delivery",
    )
    setup_parser.add_argument(
        "--interval", type=int, default=5,
        help="Cron polling interval in minutes (default: 5)",
    )
    setup_parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview all changes without writing anything",
    )

    # cron-setup
    cron_parser = sub.add_parser(
        "cron-setup",
        help="Install or update the OpenClaw cron job for notification polling",
    )
    cron_parser.add_argument(
        "--qq-id", required=True,
        help="QQ ID (hex string) for notification delivery via QQBot",
    )
    cron_parser.add_argument(
        "--interval", type=int, default=5,
        help="Polling interval in minutes (default: 5, min: 1)",
    )
    cron_parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the job JSON without writing to disk",
    )
    cron_parser.add_argument(
        "--job-id", default=_DEFAULT_CRON_JOB_ID,
        help=f"Cron job identifier (default: {_DEFAULT_CRON_JOB_ID})",
    )

    args = parser.parse_args()

    if args.command == "setup":
        cmd_setup(
            qq_id=getattr(args, "qq_id", "") or "",
            interval_minutes=args.interval,
            dry_run=args.dry_run,
        )
    elif args.command == "daemon":
        if args.action == "start":
            cmd_daemon_start()
        elif args.action == "stop":
            cmd_daemon_stop()
        elif args.action == "status":
            cmd_daemon_status()
    elif args.command == "notifications":
        cmd_notifications(json_output=args.json, ack=args.ack)
    elif args.command == "ack":
        cmd_ack(args.ids)
    elif args.command == "cron-setup":
        cmd_cron_setup(
            qq_id=args.qq_id,
            interval_minutes=args.interval,
            dry_run=args.dry_run,
            job_id=args.job_id,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
