"""Plan Tracker CLI — command-line management tool.

Provides a full headless API for plan-tracker that does not depend on
MCP or any specific AI platform.  Usable from cron jobs, shell scripts,
CI/CD pipelines, and any environment that can spawn a Python process.

Usage:
  python -m plan_tracker.cli plan create --name ... --title ... --goal ... --target-end-date ...
  python -m plan_tracker.cli plan get --name <plan>
  python -m plan_tracker.cli plan list
  python -m plan_tracker.cli plan update --name <plan> --title ... --goal ...
  python -m plan_tracker.cli plan delete --name <plan>
  python -m plan_tracker.cli plan analysis --name <plan>

  python -m plan_tracker.cli milestone add --plan ... --title ... --target-date ... --effort-hours ...
  python -m plan_tracker.cli milestone update --plan ... --id ... --title ...
  python -m plan_tracker.cli milestone current --plan ...
  python -m plan_tracker.cli milestone upcoming --plan ... [--days 7]

  python -m plan_tracker.cli checkin add --plan ... --milestone ... --progress ... [--hours ...] [--notes ...] [--blockers ...] [--morale ...]

  python -m plan_tracker.cli daily status --plan ...
  python -m plan_tracker.cli daily confirm --plan ... --status completed|partial|incomplete [--notes ...]

  python -m plan_tracker.cli reminder configure --plan ... [--enabled ...] [--before-due-days ...] ...
  python -m plan_tracker.cli reminder toggle --plan ... --enabled true|false
  python -m plan_tracker.cli reminder check-now

  python -m plan_tracker.cli notification fetch
  python -m plan_tracker.cli notification ack <id...>
  python -m plan_tracker.cli deliver

All data commands output JSON to stdout and errors to stderr.
Exit code 0 on success, 1 on failure.
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

# launchd plist
_LAUNCHD_LABEL = "com.plan-tracker.daemon"
_LAUNCHD_PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
_LAUNCHD_PLIST_PATH = _LAUNCHD_PLIST_DIR / f"{_LAUNCHD_LABEL}.plist"

_LAUNCHD_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>Comment</key>
    <string>Plan Tracker reminder daemon — keeps plans checked and notifications flowing</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ExitTimeOut</key>
    <integer>5</integer>
    <key>ProcessType</key>
    <string>Background</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>plan_tracker.daemon</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONPATH</key>
        <string>{pkg_path}</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>{data_dir}</string>
    <key>StandardOutPath</key>
    <string>{log_dir}/daemon-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/daemon-stderr.log</string>
</dict>
</plist>"""


# ── JSON helpers ──────────────────────────────────────────────────

def _ok(data=None, **extra) -> str:
    """Return a JSON success response."""
    result = {"success": True}
    if data is not None:
        result["data"] = data
    result.update(extra)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _err(message: str) -> str:
    """Return a JSON error response."""
    return json.dumps({"success": False, "error": message}, ensure_ascii=False, indent=2)


def _emit(response: str, *, ok: bool = True) -> None:
    """Print response to stdout and exit with the right code."""
    print(response)
    sys.exit(0 if ok else 1)


# ── Plan commands ─────────────────────────────────────────────────

def cmd_plan_create(args) -> None:
    from plan_tracker.plan_manager import create_plan
    try:
        plan = create_plan(
            name=args.name, title=args.title, goal=args.goal,
            target_end_date=args.target_end_date,
            category=getattr(args, "category", "custom"),
            description=getattr(args, "description", ""),
            weekly_hours_target=getattr(args, "weekly_hours", 5),
            tags=getattr(args, "tags", None),
        )
        _emit(_ok(plan))
    except ValueError as e:
        _emit(_err(str(e)), ok=False)


def cmd_plan_get(args) -> None:
    from plan_tracker.plan_manager import get_plan
    plan = get_plan(args.name)
    if plan is None:
        _emit(_err(f"Plan '{args.name}' not found"), ok=False)
    _emit(_ok(plan))


def cmd_plan_list(args) -> None:
    from plan_tracker.plan_manager import list_plans
    _emit(_ok(list_plans()))


def cmd_plan_update(args) -> None:
    from plan_tracker.plan_manager import update_plan
    updates = {}
    for field in ("title", "goal", "description", "category", "tags",
                  "target_end_date", "weekly_hours_target"):
        val = getattr(args, field, None)
        if val is not None:
            updates[field] = val
    try:
        plan = update_plan(args.name, updates)
        _emit(_ok(plan))
    except ValueError as e:
        _emit(_err(str(e)), ok=False)


def cmd_plan_delete(args) -> None:
    from plan_tracker.plan_manager import delete_plan
    ok = delete_plan(args.name)
    _emit(_ok(deleted=ok))


def cmd_plan_analysis(args) -> None:
    from plan_tracker.plan_manager import get_plan_analysis
    try:
        analysis = get_plan_analysis(args.name)
        _emit(_ok(analysis))
    except ValueError as e:
        _emit(_err(str(e)), ok=False)


# ── Milestone commands ────────────────────────────────────────────

def cmd_milestone_add(args) -> None:
    from plan_tracker.milestone_manager import add_milestone
    milestone = {}
    for field in ("title", "target_date", "effort_hours_estimate",
                  "description", "status"):
        val = getattr(args, field, None)
        if val is not None:
            milestone[field] = val
    after = getattr(args, "after", "") or ""
    try:
        result = add_milestone(args.plan, milestone, after_milestone_id=after or None)
        _emit(_ok(result))
    except ValueError as e:
        _emit(_err(str(e)), ok=False)


def cmd_milestone_update(args) -> None:
    from plan_tracker.milestone_manager import update_milestone
    updates = {}
    for field in ("title", "description", "status", "target_date",
                  "effort_hours_estimate", "notes"):
        val = getattr(args, field, None)
        if val is not None:
            updates[field] = val
    try:
        result = update_milestone(args.plan, args.id, updates)
        _emit(_ok(result))
    except ValueError as e:
        _emit(_err(str(e)), ok=False)


def cmd_milestone_current(args) -> None:
    from plan_tracker.milestone_manager import get_current_milestone
    result = get_current_milestone(args.plan)
    if result is None:
        _emit(_err(f"Plan '{args.plan}' not found or has no milestones"), ok=False)
    _emit(_ok(result))


def cmd_milestone_upcoming(args) -> None:
    from plan_tracker.milestone_manager import get_upcoming_milestones
    try:
        result = get_upcoming_milestones(args.plan, days_ahead=getattr(args, "days", 7))
        _emit(_ok(result))
    except ValueError as e:
        _emit(_err(str(e)), ok=False)


# ── Check-in commands ─────────────────────────────────────────────

def cmd_checkin_add(args) -> None:
    from plan_tracker.milestone_manager import add_checkin
    try:
        result = add_checkin(
            plan_name=args.plan,
            milestone_id=args.milestone,
            progress_pct=args.progress,
            hours_spent=getattr(args, "hours", 0),
            notes=getattr(args, "notes", ""),
            blockers=getattr(args, "blockers", ""),
            morale=getattr(args, "morale", "neutral"),
        )
        _emit(_ok(result))
    except ValueError as e:
        _emit(_err(str(e)), ok=False)


# ── Daily commands ────────────────────────────────────────────────

def cmd_daily_status(args) -> None:
    from plan_tracker.storage import load_plan
    from plan_tracker.daily_tracker import get_today_state, check_review_timeout

    plan = load_plan(args.plan)
    if plan is None:
        _emit(_err(f"Plan '{args.plan}' not found"), ok=False)

    today_state = get_today_state(args.plan)
    reminders = plan.get("reminders", {})
    timeout_minutes = reminders.get("confirmation_timeout_minutes", 10)
    is_timed_out = check_review_timeout(args.plan, timeout_minutes)

    _emit(_ok({
        "plan_name": args.plan,
        "date": today_state.get("date", ""),
        "morning_reminder": {
            "sent": today_state.get("checkin_reminded", False),
            "sent_at": today_state.get("checkin_reminded_at"),
            "configured_time": reminders.get("daily_checkin_time", "08:30"),
            "enabled": reminders.get("daily_checkin_enabled", True),
        },
        "evening_review": {
            "sent": today_state.get("review_reminded", False),
            "sent_at": today_state.get("review_reminded_at"),
            "configured_time": reminders.get("daily_review_time", "21:30"),
            "enabled": reminders.get("daily_review_enabled", True),
            "timeout_minutes": timeout_minutes,
            "is_timed_out": is_timed_out,
        },
        "confirmation": {
            "confirmed": today_state.get("confirmed", False),
            "confirmed_at": today_state.get("confirmed_at"),
            "completion_status": today_state.get("completion_status"),
            "completion_notes": today_state.get("completion_notes"),
            "auto_marked": today_state.get("auto_marked", False),
        },
    }))


def cmd_daily_confirm(args) -> None:
    from plan_tracker.daily_tracker import record_confirmation
    try:
        result = record_confirmation(
            args.plan,
            completion_status=args.status,
            notes=getattr(args, "notes", ""),
        )
        if result.get("is_archived"):
            _emit(_ok(result, message=(
                f"确认已超时，完成情况已归档到 {result['archive_target_date']}。"
                f"明天早上的提醒中将包含此信息。"
            )))
        else:
            _emit(_ok(result))
    except ValueError as e:
        _emit(_err(str(e)), ok=False)


# ── Reminder commands ─────────────────────────────────────────────

def cmd_reminder_configure(args) -> None:
    from plan_tracker.storage import load_plan, save_plan

    plan = load_plan(args.plan)
    if plan is None:
        _emit(_err(f"Plan '{args.plan}' not found"), ok=False)

    reminders = plan.setdefault("reminders", {})
    configurable = (
        "enabled", "before_due_days", "weekly_checkin_day", "weekly_checkin_time",
        "daily_checkin_time", "daily_review_time",
        "daily_checkin_enabled", "daily_review_enabled",
        "confirmation_timeout_minutes", "notification_channels",
    )
    for key in configurable:
        val = getattr(args, key.replace("-", "_"), None)
        if val is not None:
            # Convert types
            if key in ("enabled", "daily_checkin_enabled", "daily_review_enabled"):
                reminders[key] = val.lower() == "true" if isinstance(val, str) else bool(val)
            elif key in ("before_due_days", "confirmation_timeout_minutes"):
                reminders[key] = int(val)
            elif key == "notification_channels":
                reminders[key] = val.split(",")
            else:
                reminders[key] = val

    save_plan(args.plan, plan)
    _emit(_ok(reminders))


def cmd_reminder_toggle(args) -> None:
    from plan_tracker.storage import load_plan, save_plan

    plan = load_plan(args.plan)
    if plan is None:
        _emit(_err(f"Plan '{args.plan}' not found"), ok=False)

    enabled = args.enabled.lower() == "true" if isinstance(args.enabled, str) else bool(args.enabled)
    plan.setdefault("reminders", {})["enabled"] = enabled
    save_plan(args.plan, plan)
    _emit(_ok(enabled=enabled))


def cmd_reminder_check_now(args) -> None:
    from plan_tracker.reminder import check_now
    check_now()
    _emit(_ok(message="Check completed."))


# ── Notification commands ─────────────────────────────────────────

def cmd_notification_fetch(args) -> None:
    pending = fetch_all()
    _emit(_ok(notifications=pending, count=len(pending)))


def cmd_notification_ack(args) -> None:
    count = mark_delivered(args.ids)
    _emit(_ok(acknowledged=count))


# ── Daemon commands (existing) ────────────────────────────────────

def _install_launchd_plist(dry_run: bool = False) -> bool:
    """Install the launchd plist for the daemon."""
    plist_content = _LAUNCHD_PLIST_TEMPLATE.format(
        label=_LAUNCHD_LABEL,
        python=sys.executable,
        pkg_path=_detect_plan_tracker_path(),
        data_dir=str(PID_FILE.parent),
        log_dir=str(PID_FILE.parent),
    )

    if _LAUNCHD_PLIST_PATH.exists():
        current = _LAUNCHD_PLIST_PATH.read_text()
        if current.strip() == plist_content.strip():
            print("launchd plist already installed and up-to-date — skipping.")
            return False

    if dry_run:
        print(f"\nWould write launchd plist to {_LAUNCHD_PLIST_PATH}:")
        print(plist_content)
        return True

    _LAUNCHD_PLIST_DIR.mkdir(parents=True, exist_ok=True)
    _LAUNCHD_PLIST_PATH.write_text(plist_content)
    os.system(f"launchctl unload {_LAUNCHD_PLIST_PATH} 2>/dev/null")
    os.system(f"launchctl load {_LAUNCHD_PLIST_PATH}")
    print(f"✓ launchd plist installed and loaded: {_LAUNCHD_PLIST_PATH}")
    return True


def cmd_daemon_start() -> None:
    """Start the daemon in background."""
    if is_running():
        print(f"Daemon is already running (PID: {read_pid()})")
        return

    import subprocess
    daemon_script = Path(__file__).resolve().parent / "daemon.py"
    proc = subprocess.Popen(
        [sys.executable, str(daemon_script), "--daemon"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
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
    """Print pending notifications."""
    if not is_running():
        cmd_daemon_start()
    pending = fetch_all()
    if json_output:
        print(json.dumps(pending, ensure_ascii=False, indent=2))
    else:
        text = get_pending_text()
        if text:
            print(text, end="")
    if ack and pending:
        ids = [n["id"] for n in pending]
        count = mark_delivered(ids)
        print(f"Auto-acked {count} notification(s)", file=sys.stderr)


def cmd_ack(ids: list[str]) -> None:
    """Mark notifications as delivered."""
    count = mark_delivered(ids)
    print(f"Marked {count} notification(s) as delivered")


def cmd_deliver() -> None:
    """Fetch, print, and ack pending notifications in one atomic step."""
    if not is_running():
        cmd_daemon_start()
    pending = fetch_all()
    if not pending:
        return
    lines, ids = [], []
    for note in pending:
        lines.append(f"--- [{note['type']}] {note['plan_title']} ---")
        lines.append(note["message"])
        lines.append("")
        ids.append(note["id"])
    print("\n".join(lines).rstrip())
    mark_delivered(ids)


# ── Setup commands (existing) ─────────────────────────────────────

def _validate_qq_id(qq_id: str) -> str | None:
    if not qq_id or not qq_id.strip():
        return "QQ ID must not be empty"
    stripped = qq_id.strip()
    if not all(c in "0123456789ABCDEFabcdef" for c in stripped):
        return f"QQ ID '{stripped}' contains non-hex characters — please double-check"
    if len(stripped) < 4:
        return f"QQ ID '{stripped}' seems too short — please double-check"
    return None


def _webhook_launchd_label() -> str:
    return "com.plan-tracker.webhook-receiver"


def _webhook_plist_path() -> Path:
    return _LAUNCHD_PLIST_DIR / f"{_webhook_launchd_label()}.plist"


def _detect_delivery_channel() -> tuple[str, str]:
    config_path = Path.home() / ".openclaw" / "openclaw.json"
    channel, to = "qqbot", ""
    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                cfg = json.load(f)
            for ch_name, ch_cfg in cfg.get("channels", {}).items():
                if ch_cfg.get("enabled"):
                    channel = ch_name
                    break
        except (json.JSONDecodeError, OSError):
            pass
    cron_path = Path.home() / ".openclaw" / "cron" / "jobs.json"
    if cron_path.exists():
        try:
            with open(cron_path, "r") as f:
                cron_data = json.load(f)
            for job in cron_data.get("jobs", []):
                delivery = job.get("delivery", {})
                if delivery.get("channel") == channel and delivery.get("to"):
                    to = delivery["to"]
                    break
        except (json.JSONDecodeError, OSError):
            pass
    if to and not to.startswith(f"{channel}:"):
        to = f"{channel}:c2c:{to}" if channel == "qqbot" else f"{channel}:{to}"
    return channel, to


def cmd_webhook_setup(port: int = 9876, to: str = "",
                      channel: str = "", dry_run: bool = False) -> None:
    auto_channel, auto_to = _detect_delivery_channel()
    if not channel:
        channel = auto_channel
    if not to:
        to = auto_to
    if not to:
        print("Error: could not auto-detect delivery target.")
        print("  Use --to to specify it manually, e.g.:")
        print(f"  python -m plan_tracker.cli webhook-setup --to qqbot:c2c:<your-id>")
        sys.exit(1)

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "webhook_receiver.py"
    pkg_path = _detect_plan_tracker_path()
    log_dir = str(Path.home() / "mcp-servers" / "plan-tracker" / "data")

    delivery_config = {"channel": channel, "to": to}
    if not dry_run:
        from plan_tracker.storage import DATA_DIR
        config_file = DATA_DIR / "webhook_delivery.json"
        config_file.write_text(json.dumps(delivery_config, ensure_ascii=False, indent=2))

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_webhook_launchd_label()}</string>
    <key>Comment</key>
    <string>Plan Tracker webhook receiver — real-time notification delivery</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ExitTimeOut</key>
    <integer>5</integer>
    <key>ProcessType</key>
    <string>Background</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{script_path}</string>
        <string>--port</string>
        <string>{port}</string>
        <string>--channel</string>
        <string>{channel}</string>
        <string>--to</string>
        <string>{to}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONPATH</key>
        <string>{pkg_path}</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>{pkg_path}</string>
    <key>StandardOutPath</key>
    <string>{log_dir}/webhook-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/webhook-stderr.log</string>
</dict>
</plist>"""

    if dry_run:
        print(f"\nWould write launchd plist to {_webhook_plist_path()}:")
        print(plist_content)
        return

    _LAUNCHD_PLIST_DIR.mkdir(parents=True, exist_ok=True)
    _webhook_plist_path().write_text(plist_content)
    os.system(f"launchctl unload {_webhook_plist_path()} 2>/dev/null")
    os.system(f"launchctl load {_webhook_plist_path()}")
    print(f"✓ Webhook receiver installed and started")
    print(f"  Listen: http://127.0.0.1:{port}")
    print(f"  Channel: {channel}")
    print(f"  Target: {to}")


def cmd_cron_setup(
    qq_id: str = "", interval_minutes: int = 5,
    dry_run: bool = False, job_id: str = _DEFAULT_CRON_JOB_ID,
) -> None:
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
        "schedule": {"kind": "every", "everyMs": interval_ms, "anchorMs": now_ms},
        "sessionTarget": "isolated",
        "wakeMode": "now",
        "payload": {
            "kind": "agentTurn",
            "message": (
                f"exec {sys.executable} -m plan_tracker.cli deliver\n"
                "如果以上命令没有任何输出则回复NO_REPLY，否则原样转发以上命令的输出给用户"
            ),
            "timeoutSeconds": 30,
        },
        "delivery": {
            "mode": "announce", "channel": "qqbot",
            "to": f"qqbot:c2c:{qq_id.strip()}", "accountId": "default",
        },
        "state": {},
    }

    if dry_run:
        print(json.dumps(job, ensure_ascii=False, indent=2))
        print("\n# Dry-run mode — not written to disk.", file=sys.stderr)
        return

    _DEFAULT_OPENCLAW_CRON_DIR.mkdir(parents=True, exist_ok=True)
    cron_data: dict = {"version": 1, "jobs": []}
    if _DEFAULT_CRON_FILE.exists():
        try:
            with open(_DEFAULT_CRON_FILE, "r", encoding="utf-8") as fh:
                cron_data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            cron_data = {"version": 1, "jobs": []}
    if "jobs" not in cron_data or not isinstance(cron_data["jobs"], list):
        cron_data["jobs"] = []

    replaced = False
    for idx, existing in enumerate(cron_data["jobs"]):
        if existing.get("id") == job_id:
            cron_data["jobs"][idx] = job
            replaced = True
            break
    if not replaced:
        cron_data["jobs"].append(job)

    try:
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


def _detect_plan_tracker_path() -> str:
    this_dir = Path(__file__).resolve().parent
    parent = this_dir.parent
    if (parent / "pyproject.toml").exists():
        return str(parent)
    return str(this_dir.parent)


def _openclaw_config_path() -> Path | None:
    candidate = Path.home() / ".openclaw" / "openclaw.json"
    if candidate.exists():
        return candidate
    return None


def _add_mcp_server_to_config(config_path: Path, dry_run: bool = False) -> bool:
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
    tmp_path = config_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp_path.replace(config_path)
    print(f"✓ Added plan-tracker MCP server to {config_path}")
    return True


def cmd_setup(dry_run: bool = False) -> None:
    print("plan-tracker setup")
    print("=" * 40)
    config_path = _openclaw_config_path()
    if config_path:
        print(f"\n[1/2] Configuring MCP server in {config_path}...")
        _add_mcp_server_to_config(config_path, dry_run=dry_run)
    else:
        print("\n[1/2] OpenClaw config not found at ~/.openclaw/openclaw.json")
        print("       Skipping MCP server registration.")
    print(f"\n[2/2] Ensuring daemon is running...")
    if dry_run:
        print("       (dry-run — skipping daemon start)")
    elif is_running():
        print(f"       Daemon already running (PID: {read_pid()})")
    else:
        cmd_daemon_start()
    print(f"\n{'─' * 40}")
    print("Setup complete!")
    if config_path:
        print(f"  • MCP server registered in {config_path}")
    print(f"  • Daemon: {'running' if is_running() else 'pending (will auto-start on first MCP call)'}")
    print(f"  • Daemon persistence: MCP server watchdog (auto-revive every 5 min)")
    print(f"\n  Next step — set up notification polling (optional):")
    print(f"    python -m plan_tracker.cli cron-setup --help")


# ── Shared argument builders ──────────────────────────────────────

def _add_plan_name_arg(parser, help_text="Plan name (kebab-case identifier)"):
    parser.add_argument("--name", required=True, help=help_text)

def _add_json_flag(parser):
    parser.add_argument("--json", action="store_true", default=True,
                        help=argparse.SUPPRESS)  # always JSON, flag hidden


# ── main ──────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Plan Tracker CLI")
    sub = parser.add_subparsers(dest="command", help="Commands")

    # ── plan ──────────────────────────────────────────────────
    plan_parser = sub.add_parser("plan", help="Plan management")
    plan_sub = plan_parser.add_subparsers(dest="action", help="Plan actions")

    p = plan_sub.add_parser("create", help="Create a new plan")
    p.add_argument("--name", required=True, help="Plan name (kebab-case)")
    p.add_argument("--title", required=True, help="Human-readable title")
    p.add_argument("--goal", required=True, help="1-2 sentence goal description")
    p.add_argument("--target-end-date", required=True, help="Target end date (YYYY-MM-DD)")
    p.add_argument("--category", default="custom", help="learning|project|fitness|reading|custom")
    p.add_argument("--description", default="", help="Optional description")
    p.add_argument("--weekly-hours", type=int, default=5, help="Weekly hours target")
    p.add_argument("--tags", nargs="*", default=[], help="Tags")

    p = plan_sub.add_parser("get", help="Get a plan by name")
    p.add_argument("--name", required=True, help="Plan name")

    p = plan_sub.add_parser("list", help="List all plans")

    p = plan_sub.add_parser("update", help="Update plan fields")
    p.add_argument("--name", required=True, help="Plan name")
    p.add_argument("--title", help="New title")
    p.add_argument("--goal", help="New goal")
    p.add_argument("--description", help="New description")
    p.add_argument("--category", help="New category")
    p.add_argument("--tags", nargs="*", help="New tags")
    p.add_argument("--target-end-date", help="New target end date (YYYY-MM-DD)")
    p.add_argument("--weekly-hours-target", type=int, help="New weekly hours target")

    p = plan_sub.add_parser("delete", help="Delete a plan")
    p.add_argument("--name", required=True, help="Plan name")

    p = plan_sub.add_parser("analysis", help="Get plan analysis")
    p.add_argument("--name", required=True, help="Plan name")

    # ── milestone ──────────────────────────────────────────────
    ms_parser = sub.add_parser("milestone", help="Milestone management")
    ms_sub = ms_parser.add_subparsers(dest="action", help="Milestone actions")

    p = ms_sub.add_parser("add", help="Add a milestone to a plan")
    p.add_argument("--plan", required=True, help="Plan name")
    p.add_argument("--title", required=True, help="Milestone title")
    p.add_argument("--target-date", required=True, help="Target date (YYYY-MM-DD)")
    p.add_argument("--effort-hours-estimate", type=int, required=True, help="Estimated effort hours")
    p.add_argument("--description", default="", help="Optional description")
    p.add_argument("--status", default="pending", help="Initial status")
    p.add_argument("--after", default="", help="Insert after milestone ID")

    p = ms_sub.add_parser("update", help="Update a milestone")
    p.add_argument("--plan", required=True, help="Plan name")
    p.add_argument("--id", required=True, help="Milestone ID")
    p.add_argument("--title", help="New title")
    p.add_argument("--description", help="New description")
    p.add_argument("--status", help="New status")
    p.add_argument("--target-date", help="New target date")
    p.add_argument("--effort-hours-estimate", type=int, help="New effort estimate")
    p.add_argument("--notes", help="New notes")

    p = ms_sub.add_parser("current", help="Get current active milestone")
    p.add_argument("--plan", required=True, help="Plan name")

    p = ms_sub.add_parser("upcoming", help="Get upcoming milestones")
    p.add_argument("--plan", required=True, help="Plan name")
    p.add_argument("--days", type=int, default=7, help="Days ahead (default: 7)")

    # ── checkin ────────────────────────────────────────────────
    ci_parser = sub.add_parser("checkin", help="Check-in management")
    ci_sub = ci_parser.add_subparsers(dest="action", help="Check-in actions")

    p = ci_sub.add_parser("add", help="Record a check-in")
    p.add_argument("--plan", required=True, help="Plan name")
    p.add_argument("--milestone", required=True, help="Milestone ID")
    p.add_argument("--progress", type=int, required=True, help="Progress percentage (0-100)")
    p.add_argument("--hours", type=int, default=0, help="Hours spent")
    p.add_argument("--notes", default="", help="Check-in notes")
    p.add_argument("--blockers", default="", help="Blockers encountered")
    p.add_argument("--morale", default="neutral", help="Morale: struggling|neutral|good|great")

    # ── daily ──────────────────────────────────────────────────
    daily_parser = sub.add_parser("daily", help="Daily reminder / confirmation")
    daily_sub = daily_parser.add_subparsers(dest="action", help="Daily actions")

    p = daily_sub.add_parser("status", help="Get today's reminder and confirmation state")
    p.add_argument("--plan", required=True, help="Plan name")

    p = daily_sub.add_parser("confirm", help="Confirm today's plan completion")
    p.add_argument("--plan", required=True, help="Plan name")
    p.add_argument("--status", required=True, help="completion status: completed|partial|incomplete")
    p.add_argument("--notes", default="", help="Confirmation notes")

    # ── reminder ───────────────────────────────────────────────
    rem_parser = sub.add_parser("reminder", help="Reminder configuration")
    rem_sub = rem_parser.add_subparsers(dest="action", help="Reminder actions")

    p = rem_sub.add_parser("configure", help="Configure reminders for a plan")
    p.add_argument("--plan", required=True, help="Plan name")
    p.add_argument("--enabled", help="Enable/disable reminders (true/false)")
    p.add_argument("--before-due-days", type=int, help="Days before due date to start reminding")
    p.add_argument("--weekly-checkin-day", help="Weekday for weekly check-in (monday|...|sunday)")
    p.add_argument("--weekly-checkin-time", help="Time for weekly check-in (HH:MM)")
    p.add_argument("--daily-checkin-time", help="Morning check-in time (HH:MM)")
    p.add_argument("--daily-review-time", help="Evening review time (HH:MM)")
    p.add_argument("--daily-checkin-enabled", help="Enable morning check-in (true/false)")
    p.add_argument("--daily-review-enabled", help="Enable evening review (true/false)")
    p.add_argument("--confirmation-timeout-minutes", type=int, help="Review timeout in minutes")
    p.add_argument("--notification-channels", help="Comma-separated channels (e.g. mcp,webhook)")

    p = rem_sub.add_parser("toggle", help="Enable or disable reminders")
    p.add_argument("--plan", required=True, help="Plan name")
    p.add_argument("--enabled", required=True, help="true or false")

    rem_sub.add_parser("check-now", help="Trigger an immediate reminder check")

    # ── notification ───────────────────────────────────────────
    notif_parser = sub.add_parser("notification", help="Notification queue management")
    notif_sub = notif_parser.add_subparsers(dest="action", help="Notification actions")

    notif_sub.add_parser("fetch", help="Fetch pending notifications")

    p = notif_sub.add_parser("ack", help="Mark notifications as delivered")
    p.add_argument("ids", nargs="+", help="Notification IDs to ack")

    # ── daemon ─────────────────────────────────────────────────
    daemon_parser = sub.add_parser("daemon", help="Daemon management")
    daemon_parser.add_argument("action", choices=["start", "stop", "status", "install", "uninstall"])

    # ── legacy top-level commands ──────────────────────────────
    notif_legacy = sub.add_parser("notifications", help="Show pending notifications (legacy)")
    notif_legacy.add_argument("--json", action="store_true", help="Output as JSON")
    notif_legacy.add_argument("--ack", action="store_true", help="Mark as delivered after fetching")

    ack_parser = sub.add_parser("ack", help="Mark notifications as delivered (legacy)")
    ack_parser.add_argument("ids", nargs="+", help="Notification IDs to ack")

    sub.add_parser("deliver", help="Fetch, print, and ack pending notifications atomically")

    setup_parser = sub.add_parser("setup", help="One-command setup")
    setup_parser.add_argument("--dry-run", action="store_true", help="Preview without writing")

    cron_parser = sub.add_parser("cron-setup", help="Install/update OpenClaw cron job")
    cron_parser.add_argument("--qq-id", required=True, help="QQ ID (hex string)")
    cron_parser.add_argument("--interval", type=int, default=5, help="Polling interval in minutes")
    cron_parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    cron_parser.add_argument("--job-id", default=_DEFAULT_CRON_JOB_ID, help="Cron job identifier")

    webhook_parser = sub.add_parser("webhook-setup", help="Install webhook receiver")
    webhook_parser.add_argument("--port", type=int, default=9876, help="Receiver port")
    webhook_parser.add_argument("--channel", default="", help="Delivery channel")
    webhook_parser.add_argument("--to", default="", help="Delivery target")
    webhook_parser.add_argument("--dry-run", action="store_true", help="Preview without writing")

    # ── dispatch ───────────────────────────────────────────────
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    # plan
    if args.command == "plan":
        {"create": cmd_plan_create, "get": cmd_plan_get, "list": cmd_plan_list,
         "update": cmd_plan_update, "delete": cmd_plan_delete, "analysis": cmd_plan_analysis,
        }.get(args.action, lambda a: parser.print_help())(args)
    # milestone
    elif args.command == "milestone":
        {"add": cmd_milestone_add, "update": cmd_milestone_update,
         "current": cmd_milestone_current, "upcoming": cmd_milestone_upcoming,
        }.get(args.action, lambda a: parser.print_help())(args)
    # checkin
    elif args.command == "checkin":
        {"add": cmd_checkin_add}.get(args.action, lambda a: parser.print_help())(args)
    # daily
    elif args.command == "daily":
        {"status": cmd_daily_status, "confirm": cmd_daily_confirm,
        }.get(args.action, lambda a: parser.print_help())(args)
    # reminder
    elif args.command == "reminder":
        {"configure": cmd_reminder_configure, "toggle": cmd_reminder_toggle,
         "check-now": cmd_reminder_check_now,
        }.get(args.action, lambda a: parser.print_help())(args)
    # notification
    elif args.command == "notification":
        {"fetch": cmd_notification_fetch, "ack": cmd_notification_ack,
        }.get(args.action, lambda a: parser.print_help())(args)
    # daemon
    elif args.command == "daemon":
        if args.action == "start":
            cmd_daemon_start()
        elif args.action == "stop":
            cmd_daemon_stop()
        elif args.action == "status":
            cmd_daemon_status()
        elif args.action == "install":
            _install_launchd_plist()
        elif args.action == "uninstall":
            if _LAUNCHD_PLIST_PATH.exists():
                os.system(f"launchctl unload {_LAUNCHD_PLIST_PATH} 2>/dev/null")
                _LAUNCHD_PLIST_PATH.unlink()
                print(f"✓ launchd plist removed: {_LAUNCHD_PLIST_PATH}")
            else:
                print("launchd plist not installed.")
    # legacy
    elif args.command == "notifications":
        cmd_notifications(json_output=args.json, ack=args.ack)
    elif args.command == "ack":
        cmd_ack(args.ids)
    elif args.command == "deliver":
        cmd_deliver()
    elif args.command == "cron-setup":
        cmd_cron_setup(qq_id=args.qq_id, interval_minutes=args.interval,
                       dry_run=args.dry_run, job_id=args.job_id)
    elif args.command == "webhook-setup":
        cmd_webhook_setup(port=args.port, to=args.to, channel=args.channel,
                          dry_run=args.dry_run)
    elif args.command == "setup":
        cmd_setup(dry_run=args.dry_run)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
