"""Milestone lifecycle management and check-in operations."""

from datetime import datetime, timezone

from storage import load_plan, save_plan, update_index_entry

VALID_STATUSES = ("pending", "in_progress", "completed", "blocked")
VALID_MORALE = ("struggling", "neutral", "good", "great")


def add_milestone(plan_name: str, milestone: dict) -> dict:
    """Add a new milestone to a plan."""
    plan = load_plan(plan_name)
    if plan is None:
        raise ValueError(f"Plan '{plan_name}' not found")

    milestones = plan.setdefault("milestones", [])
    next_order = len(milestones) + 1
    milestone["id"] = f"ms-{next_order:03d}"
    milestone.setdefault("order", next_order)
    milestone.setdefault("status", "pending")
    milestone.setdefault("description", "")
    milestone.setdefault("actual_date", None)
    milestone.setdefault("completion_pct", 0)
    milestone.setdefault("effort_hours_estimate", 0)
    milestone.setdefault("effort_hours_actual", None)
    milestone.setdefault("notes", "")
    milestone.setdefault("checkins", [])

    if milestone["status"] not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {milestone['status']}")

    milestones.append(milestone)
    save_plan(plan_name, plan)
    update_index_entry(plan_name, plan)
    return milestone


def update_milestone(plan_name: str, milestone_id: str, updates: dict) -> dict:
    """Update milestone fields."""
    plan = load_plan(plan_name)
    if plan is None:
        raise ValueError(f"Plan '{plan_name}' not found")

    milestone = _find_milestone(plan, milestone_id)
    updatable = (
        "title", "description", "status", "target_date",
        "effort_hours_estimate", "notes",
    )
    for key in updatable:
        if key in updates:
            if key == "status" and updates[key] not in VALID_STATUSES:
                raise ValueError(f"Invalid status: {updates[key]}")
            milestone[key] = updates[key]

    save_plan(plan_name, plan)
    update_index_entry(plan_name, plan)
    return milestone


def add_checkin(
    plan_name: str,
    milestone_id: str,
    progress_pct: int,
    hours_spent: int = 0,
    notes: str = "",
    blockers: str = "",
    morale: str = "neutral",
) -> dict:
    """Record a check-in for a milestone and auto-update status."""
    plan = load_plan(plan_name)
    if plan is None:
        raise ValueError(f"Plan '{plan_name}' not found")
    if morale not in VALID_MORALE:
        raise ValueError(f"Invalid morale: {morale}")

    milestone = _find_milestone(plan, milestone_id)

    # Clamp progress
    progress_pct = max(0, min(100, progress_pct))

    checkin = {
        "date": datetime.now(timezone.utc).isoformat(),
        "progress_pct": progress_pct,
        "hours_spent": hours_spent,
        "notes": notes,
        "blockers": blockers,
        "morale": morale,
    }
    milestone.setdefault("checkins", []).append(checkin)

    milestone["completion_pct"] = progress_pct

    # Update actual hours
    current_actual = milestone.get("effort_hours_actual") or 0
    milestone["effort_hours_actual"] = current_actual + hours_spent

    # Auto-transition status
    if progress_pct >= 100:
        milestone["status"] = "completed"
        milestone["actual_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    elif progress_pct > 0 and milestone["status"] == "pending":
        milestone["status"] = "in_progress"

    if blockers:
        milestone["notes"] = (
            f"{milestone.get('notes', '')}\n[blocker] {blockers}".strip()
        )

    save_plan(plan_name, plan)
    update_index_entry(plan_name, plan)
    return milestone


def get_current_milestone(plan_name: str) -> dict | None:
    """Get the first in-progress or first pending milestone."""
    plan = load_plan(plan_name)
    if plan is None:
        return None

    for m in plan.get("milestones", []):
        if m["status"] == "in_progress":
            return m
    for m in plan.get("milestones", []):
        if m["status"] == "pending":
            return m
    return None


def get_upcoming_milestones(plan_name: str, days_ahead: int = 7) -> list[dict]:
    """Get milestones that are upcoming or overdue."""
    plan = load_plan(plan_name)
    if plan is None:
        raise ValueError(f"Plan '{plan_name}' not found")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_dt = datetime.now(timezone.utc)
    result = []

    for m in plan.get("milestones", []):
        if m["status"] in ("completed", "blocked"):
            continue
        target = m.get("target_date", "")
        if not target:
            continue
        try:
            target_dt = datetime.fromisoformat(target).replace(tzinfo=timezone.utc)
            diff = (target_dt - today_dt).days
        except (ValueError, TypeError):
            continue

        if diff <= days_ahead:
            result.append({
                "plan_name": plan_name,
                "milestone_id": m["id"],
                "milestone_title": m["title"],
                "status": m["status"],
                "target_date": target,
                "days_remaining": diff,
                "completion_pct": m.get("completion_pct", 0),
                "is_overdue": diff < 0,
                "is_stale": _is_checkin_stale(m),
            })

    return result


def _find_milestone(plan: dict, milestone_id: str) -> dict:
    """Find a milestone by id, raise if not found."""
    for m in plan.get("milestones", []):
        if m["id"] == milestone_id:
            return m
    raise ValueError(f"Milestone '{milestone_id}' not found in plan")


def _is_checkin_stale(milestone: dict, max_days: int = 7) -> bool:
    """Check if the most recent check-in is older than max_days."""
    checkins = milestone.get("checkins", [])
    if not checkins:
        return False
    last = checkins[-1]
    try:
        last_dt = datetime.fromisoformat(last["date"]).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_dt).days > max_days
    except (ValueError, KeyError):
        return False
