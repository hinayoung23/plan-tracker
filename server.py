"""Plan Tracker MCP Server.

Provides tools for managing long-term plans with milestones,
check-ins, progress analysis, and scheduled reminders.
"""

import json
import logging
import sys
from pathlib import Path

# Ensure the server directory is on sys.path for imports
SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from mcp.server.fastmcp import FastMCP

from plan_manager import (
    create_plan,
    get_plan,
    list_plans,
    update_plan,
    delete_plan,
    get_plan_analysis,
)
from milestone_manager import (
    add_milestone,
    update_milestone,
    add_checkin,
    get_current_milestone,
    get_upcoming_milestones,
)
from notification_queue import fetch_all, mark_delivered
from daily_tracker import (
    get_today_state,
    record_confirmation,
    check_review_timeout,
    auto_mark_incomplete,
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
async def milestone_add(plan_name: str, milestone: dict) -> str:
    """Add a milestone to a plan. milestone keys: title, target_date, effort_hours_estimate (required); description, status (optional)."""
    try:
        result = add_milestone(plan_name, milestone)
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
    from storage import load_plan, save_plan

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
    from storage import load_plan, save_plan

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
    from storage import load_plan

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
    """Configure email notifications (premium feature). config: enabled, api_url, api_key, recipient."""
    from storage import load_plan, save_plan

    plan = load_plan(plan_name)
    if plan is None:
        return _json_response({"success": False, "error": f"Plan '{plan_name}' not found"})

    email = plan.setdefault("reminders", {}).setdefault("email", {})
    for key in ("enabled", "api_url", "api_key", "recipient"):
        if key in config:
            email[key] = config[key]

    channels = plan["reminders"].setdefault("notification_channels", ["mcp"])
    if "email" not in channels:
        channels.append("email")

    save_plan(plan_name, plan)
    return _json_response({"success": True, "email": email})


@mcp.tool()
async def reminder_check_now() -> str:
    """Manually trigger an immediate check for upcoming/overdue milestones."""
    reminder.check_now()
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


def main():
    # Reminder engine now runs in the standalone daemon (daemon.py).
    # The MCP server only handles tool calls.
    mcp.run()


if __name__ == "__main__":
    main()
