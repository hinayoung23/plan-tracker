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
    validate_plan_name,
    sanitize_plan,
    _compute_plan_status,
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
    validate_plan_name(name)
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
        "weekly_hours_target": max(0, weekly_hours_target),
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
            "notification_channels": ["mcp", "webhook"],
            "email": {
                "enabled": False,
                "api_url": "https://mail.tempbox.cn/api/send-email",
                "api_key_id": "",
                "api_secret": "",
                "recipient": "",
            },
            "webhook": {
                "url": "http://127.0.0.1:9876",
            },
        },
    }

    save_plan(name, plan)
    update_index_entry(name, plan)
    return sanitize_plan(plan)


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
        m.setdefault("effort_hours_estimate", max(0, m.get("effort_hours_estimate", 0)))
        m.setdefault("effort_hours_actual", None)
        m.setdefault("notes", "")
        m.setdefault("checkins", [])
        result.append(m)
    return result


def get_plan(plan_name: str) -> dict | None:
    validate_plan_name(plan_name)
    plan = load_plan(plan_name)
    if plan is None:
        return None
    return sanitize_plan(plan)


def list_plans() -> list[dict]:
    return load_index().get("plans", [])


def update_plan(plan_name: str, updates: dict) -> dict:
    """Update top-level plan fields. Raises ValueError if plan not found."""
    validate_plan_name(plan_name)
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
    return sanitize_plan(plan)


def delete_plan(plan_name: str) -> bool:
    """Delete a plan and all associated data."""
    validate_plan_name(plan_name)
    plan = load_plan(plan_name)
    if plan is None:
        return False

    # Clean up associated data
    try:
        from plan_tracker.notification_queue import remove_for_plan
        removed_q = remove_for_plan(plan_name)
    except Exception:
        removed_q = 0

    try:
        from plan_tracker.daily_tracker import remove_for_plan as remove_daily
        removed_d = remove_daily(plan_name)
    except Exception:
        removed_d = 0

    try:
        from plan_tracker.reminder import remove_plan_state
        remove_plan_state(plan_name)
    except Exception:
        pass

    delete_plan_file(plan_name)
    remove_index_entry(plan_name)
    return True


def get_plan_analysis(plan_name: str) -> dict:
    """Compute statistics for a plan: pace, deviation, trends."""
    validate_plan_name(plan_name)
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
            # Interpret target_end_date as end-of-day (23:59:59)
            target_date = target_date.replace(hour=23, minute=59, second=59)
            created = datetime.fromisoformat(plan["created_at"])
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
        "status": _compute_plan_status(plan),
    }
