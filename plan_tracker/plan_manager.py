"""Plan-level CRUD operations.

Validates plan structure, manages plan index, delegates milestone operations.
"""

from datetime import datetime, timezone

from plan_tracker.storage import (
    DATA_DIR,
    INDEX_FILE,
    load_plan,
    save_plan,
    plan_path,
    modify_plan_and_index,
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


def _reschedule() -> None:
    """Touch the reschedule marker so the daemon picks up new/changed plans."""
    try:
        from plan_tracker.reminder import RESCHEDULE_MARKER
        RESCHEDULE_MARKER.parent.mkdir(parents=True, exist_ok=True)
        RESCHEDULE_MARKER.touch()
    except Exception:
        pass


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
    if weekly_hours_target < 0:
        raise ValueError("weekly_hours_target must be >= 0")

    now = datetime.now(timezone.utc).isoformat()

    plan_data = {
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

    # Atomic create: modify_plan_and_index with create=True handles
    # the file creation, population, and index update under dual locks.
    def _populate(plan):
        if plan.get("name"):
            raise ValueError(f"Plan '{name}' already exists")
        plan.update(plan_data)

    plan = modify_plan_and_index(name, _populate, create=True)
    _reschedule()
    return sanitize_plan(plan)


def _init_milestones(raw: list[dict]) -> list[dict]:
    """Validate and initialize milestone list with defaults."""
    # Validate basic fields first
    for i, m in enumerate(raw):
        if not m.get("title"):
            raise ValueError(f"Milestone {i+1} must have a title")
        if not m.get("target_date"):
            raise ValueError(f"Milestone '{m['title']}' must have a target_date")
        effort = m.get("effort_hours_estimate", 0)
        if not isinstance(effort, (int, float)) or effort < 0:
            raise ValueError(f"Milestone '{m['title']}' effort_hours_estimate must be >= 0")
        if m.get("status", "pending") not in VALID_STATUSES:
            raise ValueError(f"Invalid milestone status: {m.get('status')}")

    # Assign IDs, checking for collisions between auto-generated and explicit
    all_ids = set()
    result = []
    for i, m in enumerate(raw):
        if "id" in m:
            mid = m["id"]
            if not isinstance(mid, str):
                raise ValueError(f"Milestone ID must be a string, got {type(mid).__name__}: {mid!r}")
        else:
            mid = f"ms-{i + 1:03d}"
            m["id"] = mid
        if mid in all_ids:
            raise ValueError(f"Duplicate milestone ID '{mid}' is not allowed")
        all_ids.add(mid)
        m.setdefault("status", "pending")
        m.setdefault("order", i + 1)
        m.setdefault("description", "")
        m.setdefault("actual_date", None)
        m.setdefault("completion_pct", 0)
        m.setdefault("effort_hours_estimate", int(m.get("effort_hours_estimate", 0)))
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
    """Update top-level plan fields (atomic)."""
    validate_plan_name(plan_name)
    for key in updates:
        if key == "category" and updates[key] not in CATEGORIES:
            raise ValueError(f"Invalid category: {updates[key]}")
        if key == "weekly_hours_target" and updates[key] < 0:
            raise ValueError("weekly_hours_target must be >= 0")

    def _do(plan):
        updatable = ("title", "goal", "description", "category", "tags",
                    "target_end_date", "weekly_hours_target")
        for key in updatable:
            if key in updates:
                plan[key] = updates[key]

    plan = modify_plan_and_index(plan_name, _do)
    _reschedule()
    return sanitize_plan(dict(plan))


def delete_plan(plan_name: str) -> bool:
    """Delete a plan and all associated data. Holds index lock to
    prevent delete+recreate races."""
    validate_plan_name(plan_name)
    import logging
    _log = logging.getLogger("plan_tracker.plan_manager")
    from plan_tracker.file_lock import LockedFile

    # Acquire index lock FIRST so no concurrent create can sneak in
    # between our delete and the index update.
    with LockedFile(INDEX_FILE, default={"plans": []}) as index:
        # Check existence under lock
        if not plan_path(plan_name).exists():
            return False

        # Remove from index before deleting the file
        index["plans"] = [p for p in index["plans"] if p["name"] != plan_name]

        # Delete plan file
        delete_plan_file(plan_name)

        # Clean up associated data
        try:
            from plan_tracker.notification_queue import remove_for_plan
            remove_for_plan(plan_name)
        except Exception:
            _log.exception("Failed to clean notification queue for %s", plan_name)
        try:
            from plan_tracker.daily_tracker import remove_for_plan as remove_daily
            remove_daily(plan_name)
        except Exception:
            _log.exception("Failed to clean daily state for %s", plan_name)
        try:
            from plan_tracker.reminder import remove_plan_state
            remove_plan_state(plan_name)
        except Exception:
            _log.exception("Failed to clean reminder state for %s", plan_name)

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
