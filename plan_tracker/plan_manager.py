"""Plan-level CRUD operations.

Validates plan structure, manages plan index, delegates milestone operations.
"""

from datetime import datetime, timezone

from plan_tracker.storage import (
    load_plan,
    save_plan,
    delete_plan_file,
    update_index_entry,
    remove_index_entry,
    load_index,
)

CATEGORIES = ("learning", "project", "fitness", "reading", "custom")
VALID_STATUSES = ("pending", "in_progress", "completed", "blocked")
VALID_MORALE = ("struggling", "neutral", "good", "great")


def create_plan(
    name: str,
    title: str,
    goal: str,
    target_end_date: str,
    category: str = "custom",
    description: str = "",
    weekly_hours_target: int = 5,
    tags: list[str] | None = None,
    milestones: list[dict] | None = None,
) -> dict:
    """Create a new plan. Returns the created plan dict or raises ValueError."""
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Plan name must be kebab-case alphanumeric")
    if category not in CATEGORIES:
        raise ValueError(f"Category must be one of: {CATEGORIES}")
    if load_plan(name) is not None:
        raise ValueError(f"Plan '{name}' already exists")

    now = datetime.now(timezone.utc).isoformat()

    plan = {
        "name": name,
        "title": title,
        "goal": goal,
        "description": description,
        "category": category,
        "tags": tags or [],
        "created_at": now,
        "updated_at": now,
        "target_end_date": target_end_date,
        "weekly_hours_target": weekly_hours_target,
        "milestones": _init_milestones(milestones or []),
        "reminders": {
            "enabled": True,
            "before_due_days": 3,
            "weekly_checkin_day": "monday",
            "weekly_checkin_time": "09:00",
            "daily_checkin_time": "08:30",
            "daily_review_time": "21:30",
            "daily_checkin_enabled": True,
            "daily_review_enabled": True,
            "confirmation_timeout_minutes": 10,
            "notification_channels": ["mcp"],
            "email": {
                "enabled": False,
                "api_url": "http://mail.tempbox.cn/api/send-email",
                "api_key": "plan-tracker-api-key",
                "recipient": "",
            },
        },
    }

    save_plan(name, plan)
    update_index_entry(name, plan)
    return plan


def _init_milestones(raw: list[dict]) -> list[dict]:
    """Validate and initialize milestone list with defaults."""
    result = []
    for i, m in enumerate(raw):
        if "id" not in m:
            m["id"] = f"ms-{i + 1:03d}"
        m.setdefault("status", "pending")
        if m["status"] not in VALID_STATUSES:
            raise ValueError(f"Invalid milestone status: {m['status']}")
        m.setdefault("order", i + 1)
        m.setdefault("description", "")
        m.setdefault("actual_date", None)
        m.setdefault("completion_pct", 0)
        m.setdefault("effort_hours_estimate", 0)
        m.setdefault("effort_hours_actual", None)
        m.setdefault("notes", "")
        m.setdefault("checkins", [])
        result.append(m)
    return result


def get_plan(plan_name: str) -> dict | None:
    return load_plan(plan_name)


def list_plans() -> list[dict]:
    return load_index().get("plans", [])


def update_plan(plan_name: str, updates: dict) -> dict:
    """Update top-level plan fields. Raises ValueError if plan not found."""
    plan = load_plan(plan_name)
    if plan is None:
        raise ValueError(f"Plan '{plan_name}' not found")

    updatable = (
        "title", "goal", "description", "category", "tags",
        "target_end_date", "weekly_hours_target",
    )
    for key in updatable:
        if key in updates:
            if key == "category" and updates[key] not in CATEGORIES:
                raise ValueError(f"Invalid category: {updates[key]}")
            plan[key] = updates[key]

    save_plan(plan_name, plan)
    update_index_entry(plan_name, plan)
    return plan


def delete_plan(plan_name: str) -> bool:
    """Delete a plan and remove from index. Returns True if deleted."""
    plan = load_plan(plan_name)
    if plan is None:
        return False
    delete_plan_file(plan_name)
    remove_index_entry(plan_name)
    return True


def get_plan_analysis(plan_name: str) -> dict:
    """Compute statistics for a plan: pace, deviation, trends."""
    plan = load_plan(plan_name)
    if plan is None:
        raise ValueError(f"Plan '{plan_name}' not found")

    milestones = plan.get("milestones", [])
    now = datetime.now(timezone.utc)

    total_est = sum(m.get("effort_hours_estimate", 0) for m in milestones)
    total_actual = sum(
        m.get("effort_hours_actual", 0)
        for m in milestones
        if m.get("effort_hours_actual") is not None
    )

    completed = [m for m in milestones if m["status"] == "completed"]
    in_progress = [m for m in milestones if m["status"] == "in_progress"]
    pending = [m for m in milestones if m["status"] == "pending"]
    blocked = [m for m in milestones if m["status"] == "blocked"]

    paces = []
    for m in completed:
        if m.get("effort_hours_estimate") and m.get("effort_hours_actual"):
            paces.append(m["effort_hours_actual"] / m["effort_hours_estimate"])

    avg_pace = round(sum(paces) / len(paces), 2) if paces else 1.0

    remaining_est = sum(
        m.get("effort_hours_estimate", 0)
        for m in milestones
        if m["status"] != "completed"
    )
    adjusted_remaining = round(remaining_est * avg_pace, 1)

    target = plan.get("target_end_date", "")
    days_total = 0
    days_remaining = 0
    days_elapsed = 0
    if target:
        try:
            target_date = datetime.fromisoformat(target).replace(tzinfo=timezone.utc)
            created = datetime.fromisoformat(plan["created_at"]).replace(tzinfo=timezone.utc)
            days_total = max((target_date - created).days, 1)
            days_remaining = max((target_date - now).days, 0)
            days_elapsed = min((now - created).days, days_total)
        except (ValueError, KeyError):
            pass

    time_elapsed_pct = round(days_elapsed / days_total * 100) if days_total else 0
    progress_pct = round(len(completed) / max(len(milestones), 1) * 100)

    morale_trend = []
    for m in milestones:
        for c in m.get("checkins", []):
            if c.get("morale"):
                morale_trend.append(c["morale"])

    return {
        "total_milestones": len(milestones),
        "completed": len(completed),
        "in_progress": len(in_progress),
        "pending": len(pending),
        "blocked": len(blocked),
        "total_est_hours": total_est,
        "total_actual_hours": total_actual,
        "average_pace": avg_pace,
        "remaining_est_hours": remaining_est,
        "adjusted_remaining_hours": adjusted_remaining,
        "days_total": days_total,
        "days_remaining": days_remaining,
        "days_elapsed": days_elapsed,
        "time_elapsed_pct": time_elapsed_pct,
        "progress_pct": progress_pct,
        "morale_trend": morale_trend[-5:],
        "status": _plan_status(plan),
    }


def _plan_status(plan: dict) -> str:
    completed = sum(1 for m in plan.get("milestones", []) if m["status"] == "completed")
    total = len(plan.get("milestones", [])) or 1
    if completed == total:
        return "completed"
    if any(m["status"] == "blocked" for m in plan.get("milestones", [])):
        return "behind"
    if not any(m["status"] == "in_progress" for m in plan.get("milestones", [])):
        return "paused"
    # Check if any active milestone is past due
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for m in plan.get("milestones", []):
        if m["status"] in ("in_progress", "pending") and m.get("target_date", "") < today:
            return "behind"
    return "on_track"
