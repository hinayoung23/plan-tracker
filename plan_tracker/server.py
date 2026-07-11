"""Plan Tracker MCP Server.

Provides tools for managing long-term plans with milestones,
check-ins, progress analysis, and scheduled reminders.

On startup, ensures the plan-tracker daemon is running and monitors
its health, restarting it automatically if it dies.
"""

import fcntl
import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from plan_tracker.plan_manager import (
    create_plan,
    get_plan,
    list_plans,
    update_plan,
    delete_plan,
    get_plan_analysis,
)
from plan_tracker.milestone_manager import (
    add_milestone,
    update_milestone,
    add_checkin,
    get_current_milestone,
    get_upcoming_milestones,
)
from plan_tracker.notification_queue import fetch_all, mark_delivered
from plan_tracker.reminder import check_now as reminder_check_now_impl
from plan_tracker.daily_tracker import (
    get_today_state,
    record_confirmation,
    check_review_timeout,
    auto_mark_incomplete,
    catch_up_past_timeouts,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("plan_tracker.server")

mcp = FastMCP("plan-tracker")


def _json_response(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


# ── Plan tools ──

@mcp.tool()
async def plan_create(
    name: str,
    title: str,
    goal: str,
    target_end_date: str,
    category: str = "custom",
    description: str = "",
    weekly_hours_target: int = 5,
    tags: list[str] | None = None,
    milestones: list[dict] | None = None,
) -> str:
    """Create a new plan. name must be kebab-case. category: learning/project/fitness/reading/custom."""
    try:
        plan = create_plan(
            name=name, title=title, goal=goal,
            target_end_date=target_end_date, category=category,
            description=description, weekly_hours_target=weekly_hours_target,
            tags=tags, milestones=milestones,
        )
        return _json_response({"success": True, "plan": plan})
    except ValueError as e:
        return _json_response({"success": False, "error": str(e)})


@mcp.tool()
async def plan_get(plan_name: str) -> str:
    """Get a plan by name."""
    plan = get_plan(plan_name)
    if plan is None:
        return _json_response({"success": False, "error": f"Plan '{plan_name}' not found"})
    return _json_response({"success": True, "plan": plan})


@mcp.tool()
async def plan_list() -> str:
    """List all plans."""
    plans = list_plans()
    return _json_response({"success": True, "plans": plans})


@mcp.tool()
async def plan_update(plan_name: str, updates: dict) -> str:
    """Update plan fields. updatable: title, goal, description, category, tags, target_end_date, weekly_hours_target."""
    try:
        plan = update_plan(plan_name, updates)
        return _json_response({"success": True, "plan": plan})
    except ValueError as e:
        return _json_response({"success": False, "error": str(e)})


@mcp.tool()
async def plan_delete(plan_name: str) -> str:
    """Delete a plan."""
    ok = delete_plan(plan_name)
    return _json_response({"success": ok, "deleted": ok})


@mcp.tool()
async def plan_analysis(plan_name: str) -> str:
    """Get plan statistics: pace, deviation, progress, morale trends."""
    try:
        analysis = get_plan_analysis(plan_name)
        return _json_response({"success": True, "analysis": analysis})
    except ValueError as e:
        return _json_response({"success": False, "error": str(e)})


# ── Milestone tools ──

@mcp.tool()
async def milestone_add(plan_name: str, milestone: dict,
                        after_milestone_id: str = "") -> str:
    """Add a milestone to a plan. milestone keys: title, target_date, effort_hours_estimate (required); description, status (optional). Use after_milestone_id to insert after a specific milestone instead of appending to the end."""
    try:
        # Support passing after_milestone_id either as explicit param or inside milestone dict
        position = after_milestone_id or milestone.pop("after_milestone_id", "") or None
        result = add_milestone(plan_name, milestone,
                               after_milestone_id=position)
        return _json_response({"success": True, "milestone": result})
    except ValueError as e:
        return _json_response({"success": False, "error": str(e)})


@mcp.tool()
async def milestone_update(plan_name: str, milestone_id: str, updates: dict) -> str:
    """Update milestone fields. updatable: title, description, status, target_date, effort_hours_estimate, notes."""
    try:
        result = update_milestone(plan_name, milestone_id, updates)
        return _json_response({"success": True, "milestone": result})
    except ValueError as e:
        return _json_response({"success": False, "error": str(e)})


@mcp.tool()
async def milestone_current(plan_name: str) -> str:
    """Get the current active milestone (first in_progress or first pending)."""
    result = get_current_milestone(plan_name)
    if result is None:
        return _json_response({"success": False, "error": f"Plan '{plan_name}' not found or has no milestones"})
    return _json_response({"success": True, "milestone": result})


@mcp.tool()
async def milestone_upcoming(plan_name: str, days_ahead: int = 7) -> str:
    """Get milestones due within days_ahead, including overdue ones."""
    try:
        result = get_upcoming_milestones(plan_name, days_ahead)
        return _json_response({"success": True, "upcoming": result})
    except ValueError as e:
        return _json_response({"success": False, "error": str(e)})


# ── Check-in tools ──

@mcp.tool()
async def checkin_add(
    plan_name: str,
    milestone_id: str,
    progress_pct: int,
    hours_spent: int = 0,
    notes: str = "",
    blockers: str = "",
    morale: str = "neutral",
) -> str:
    """Record a check-in for a milestone. Auto-updates milestone status and completion."""
    try:
        result = add_checkin(
            plan_name=plan_name, milestone_id=milestone_id,
            progress_pct=progress_pct, hours_spent=hours_spent,
            notes=notes, blockers=blockers, morale=morale,
        )
        return _json_response({"success": True, "milestone": result})
    except ValueError as e:
        return _json_response({"success": False, "error": str(e)})


# ── Reminder tools ──

@mcp.tool()
async def reminder_configure(plan_name: str, config: dict) -> str:
    """Configure reminders for a plan. config keys: enabled, before_due_days, weekly_checkin_day, weekly_checkin_time, daily_checkin_time, daily_review_time, daily_checkin_enabled, daily_review_enabled, confirmation_timeout_minutes, notification_channels."""
    from plan_tracker.storage import load_plan, save_plan

    plan = load_plan(plan_name)
    if plan is None:
        return _json_response({"success": False, "error": f"Plan '{plan_name}' not found"})

    reminders = plan.setdefault("reminders", {})
    configurable = (
        "enabled", "before_due_days", "weekly_checkin_day", "weekly_checkin_time",
        "daily_checkin_time", "daily_review_time",
        "daily_checkin_enabled", "daily_review_enabled",
        "confirmation_timeout_minutes", "notification_channels",
    )
    for key in configurable:
        if key in config:
            reminders[key] = config[key]

    save_plan(plan_name, plan)
    return _json_response({"success": True, "reminders": reminders})


@mcp.tool()
async def reminder_toggle(plan_name: str, enabled: bool) -> str:
    """Enable or disable reminders for a plan."""
    from plan_tracker.storage import load_plan, save_plan

    plan = load_plan(plan_name)
    if plan is None:
        return _json_response({"success": False, "error": f"Plan '{plan_name}' not found"})

    plan.setdefault("reminders", {})["enabled"] = enabled
    save_plan(plan_name, plan)
    return _json_response({"success": True, "enabled": enabled})


# ── Daily confirmation tools ──

@mcp.tool()
async def daily_confirm(plan_name: str, status: str, notes: str = "") -> str:
    """Confirm today's plan completion. status: completed | partial | incomplete."""
    try:
        result = record_confirmation(plan_name, completion_status=status, notes=notes)
        if result.get("is_archived"):
            return _json_response({
                "success": True,
                "message": (
                    f"确认已超时，完成情况已归档到 {result['archive_target_date']}。"
                    f"明天早上的提醒中将包含此信息。"
                ),
                "result": result,
            })
        return _json_response({"success": True, "result": result})
    except ValueError as e:
        return _json_response({"success": False, "error": str(e)})


@mcp.tool()
async def daily_status(plan_name: str) -> str:
    """Get today's reminder and confirmation status for a plan."""
    from plan_tracker.storage import load_plan

    plan = load_plan(plan_name)
    if plan is None:
        return _json_response({"success": False, "error": f"Plan '{plan_name}' not found"})

    today_state = get_today_state(plan_name)
    reminders = plan.get("reminders", {})
    timeout_minutes = reminders.get("confirmation_timeout_minutes", 10)
    is_timed_out = check_review_timeout(plan_name, timeout_minutes)

    return _json_response({
        "success": True,
        "daily_status": {
            "plan_name": plan_name,
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
        },
    })


@mcp.tool()
async def email_configure(plan_name: str, config: dict) -> str:
    """Configure email notifications via mail.tempbox.cn. config: enabled, api_url, api_key_id, api_secret, recipient."""
    from plan_tracker.storage import load_plan, save_plan

    plan = load_plan(plan_name)
    if plan is None:
        return _json_response({"success": False, "error": f"Plan '{plan_name}' not found"})

    email = plan.setdefault("reminders", {}).setdefault("email", {})
    for key in ("enabled", "api_url", "api_key_id", "api_secret", "recipient"):
        if key in config:
            email[key] = config[key]

    channels = plan["reminders"].setdefault("notification_channels", ["mcp"])
    if "email" not in channels:
        channels.append("email")

    save_plan(plan_name, plan)
    # Never expose api_secret in response
    safe = {k: v for k, v in email.items() if k != "api_secret"}
    safe["api_secret"] = "***" if email.get("api_secret") else ""
    return _json_response({"success": True, "email": safe})


@mcp.tool()
async def webhook_configure(plan_name: str, config: dict) -> str:
    """Configure webhook notifications for real-time push delivery. config: url (required)."""
    from plan_tracker.storage import load_plan, save_plan

    plan = load_plan(plan_name)
    if plan is None:
        return _json_response({"success": False, "error": f"Plan '{plan_name}' not found"})

    webhook = plan.setdefault("reminders", {}).setdefault("webhook", {})
    if "url" in config:
        webhook["url"] = config["url"]

    channels = plan["reminders"].setdefault("notification_channels", ["mcp"])
    if "webhook" not in channels:
        channels.append("webhook")

    save_plan(plan_name, plan)
    return _json_response({"success": True, "webhook": webhook})


@mcp.tool()
async def reminder_check_now() -> str:
    """Manually trigger an immediate check for upcoming/overdue milestones."""
    reminder_check_now_impl()
    return _json_response({"success": True, "message": "Check completed."})


# ── Notification queue tools ──

@mcp.tool()
async def notification_fetch() -> str:
    """Fetch pending reminder notifications from the daemon queue."""
    pending = fetch_all()
    return _json_response({
        "success": True,
        "count": len(pending),
        "notifications": pending,
    })


@mcp.tool()
async def notification_ack(notification_ids: list[str]) -> str:
    """Mark notifications as delivered after they have been sent to the user."""
    count = mark_delivered(notification_ids)
    return _json_response({
        "success": True,
        "acknowledged": count,
    })


# ── Daemon lifecycle management ──

# How often the watchdog checks whether the daemon is still alive
_DAEMON_WATCHDOG_INTERVAL = 300  # 5 minutes
# Max wait time for daemon PID file to appear after launching
_DAEMON_START_TIMEOUT = 10
# Lock file to prevent concurrent daemon starts
_DAEMON_LOCK_FILE = Path(__file__).resolve().parent.parent / "data" / "daemon.lock"


def _ensure_daemon() -> bool:
    """Start the plan-tracker daemon if it is not already running.

    Uses a file lock to prevent concurrent attempts from spawning
    multiple daemon instances.  Returns True if the daemon was
    already running or was started successfully; False if the
    daemon could not be started.
    """
    from plan_tracker.daemon import is_running, read_pid

    if is_running():
        logger.debug("Daemon already running (PID: %d)", read_pid())
        return True

    # Acquire an exclusive lock so only one caller tries to start
    _DAEMON_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    try:
        lock_fd = open(_DAEMON_LOCK_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        # Another caller is already starting the daemon — wait for it
        logger.debug("Another process is starting the daemon, waiting...")
        deadline = time.monotonic() + _DAEMON_START_TIMEOUT + 5
        while time.monotonic() < deadline:
            if is_running():
                logger.info("Daemon started by another process (PID: %d)", read_pid())
                return True
            time.sleep(0.5)
        logger.warning("Timed out waiting for another process to start daemon")
        return False

    try:
        # Double-check after acquiring lock
        if is_running():
            logger.debug("Daemon already running (PID: %d) — started after lock wait", read_pid())
            return True

        logger.info("Daemon not running — starting...")
        daemon_script = Path(__file__).resolve().parent / "daemon.py"
        proc = subprocess.Popen(
            [sys.executable, str(daemon_script), "--daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        proc.wait(timeout=5)

        # The daemon double-forks, so the direct child exits quickly.
        # Give the grandchild a moment to write its PID file.
        deadline = time.monotonic() + _DAEMON_START_TIMEOUT
        while time.monotonic() < deadline:
            if is_running():
                logger.info("Daemon started (PID: %d)", read_pid())
                return True
            time.sleep(0.5)

        logger.warning("Daemon did not appear after %ds — check daemon.log",
                       _DAEMON_START_TIMEOUT)
        return False
    except Exception:
        logger.exception("Failed to start daemon")
        return False
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
            except OSError:
                pass


def _daemon_watchdog() -> None:
    """Background thread: periodically check daemon health and restart if needed."""
    from plan_tracker.daemon import is_running

    while True:
        time.sleep(_DAEMON_WATCHDOG_INTERVAL)
        try:
            if not is_running():
                logger.warning("Watchdog: daemon has stopped — restarting...")
                _ensure_daemon()
        except Exception:
            logger.exception("Watchdog check failed")


def main():
    # Ensure the reminder daemon is running (auto-start if needed)
    _ensure_daemon()

    # Background thread that revives the daemon if it dies
    watchdog = threading.Thread(
        target=_daemon_watchdog,
        daemon=True,
        name="plan-tracker-watchdog",
    )
    watchdog.start()

    mcp.run()


if __name__ == "__main__":
    main()
