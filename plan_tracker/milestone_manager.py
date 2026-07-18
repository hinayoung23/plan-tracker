"""Milestone lifecycle management and check-in operations."""

from datetime import datetime, timezone

from plan_tracker.storage import load_plan, save_plan, modify_plan, update_index_entry, validate_plan_name

VALID_STATUSES = ("pending", "in_progress", "completed", "blocked")
VALID_MORALE = ("struggling", "neutral", "good", "great")


def _next_milestone_id(milestones: list[dict]) -> str:
    """Generate the next milestone ID without renumbering existing ones."""
    max_n = 0
    for m in milestones:
        try:
            n = int(m["id"].split("-")[-1])
            if n > max_n:
                max_n = n
        except (ValueError, IndexError):
            pass
    return f"ms-{max_n + 1:03d}"


def add_milestone(plan_name: str, milestone: dict,
                  after_milestone_id: str | None = None) -> dict:
    """Add a new milestone to a plan.

    New milestones get a fresh ID (max+1). Existing milestones are
    never renumbered, so external references remain valid.
    """
    validate_plan_name(plan_name)
    plan = load_plan(plan_name)
    if plan is None:
        raise ValueError(f"Plan '{plan_name}' not found")

    milestones = plan.setdefault("milestones", [])

    insert_at = len(milestones)
    if after_milestone_id:
        found = False
        for i, m in enumerate(milestones):
            if m["id"] == after_milestone_id:
                insert_at = i + 1
                found = True
                break
        if not found:
            raise ValueError(
                f"Milestone '{after_milestone_id}' not found in plan '{plan_name}'"
            )

    milestone["id"] = _next_milestone_id(milestones)
    milestone.setdefault("order", insert_at + 1)
    milestone.setdefault("status", "pending")
    milestone.setdefault("description", "")
    milestone.setdefault("actual_date", None)
    milestone.setdefault("completion_pct", 0)
    milestone.setdefault("effort_hours_estimate", max(0, milestone.get("effort_hours_estimate", 0)))
    milestone.setdefault("effort_hours_actual", None)
    milestone.setdefault("notes", "")
    milestone.setdefault("checkins", [])

    if not milestone.get("title"):
        raise ValueError("Milestone must have a title")
    if not milestone.get("target_date"):
        raise ValueError("Milestone must have a target_date")
    if milestone.get("effort_hours_estimate", 0) < 0:
        raise ValueError("effort_hours_estimate must be >= 0")
    if milestone["status"] not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {milestone['status']}")

    milestones.insert(insert_at, milestone)
    # Update order field for display only (IDs are immutable)
    for i, m in enumerate(milestones):
        m["order"] = i + 1

    save_plan(plan_name, plan)
    update_index_entry(plan_name, plan)
    return milestone


def update_milestone(plan_name: str, milestone_id: str, updates: dict) -> dict:
    """Update milestone fields.

    When status is changed to ``completed``, synced completion_pct to
    100 and actual_date to today if not already set.
    """
    validate_plan_name(plan_name)
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
            val = updates[key]
            if key == "status" and val not in VALID_STATUSES:
                raise ValueError(f"Invalid status: {val}")
            if key == "effort_hours_estimate" and val < 0:
                raise ValueError("effort_hours_estimate must be >= 0")
            milestone[key] = val

    # Sync state when manually set to completed
    if milestone["status"] == "completed":
        if milestone.get("completion_pct", 0) < 100:
            milestone["completion_pct"] = 100
        if not milestone.get("actual_date"):
            milestone["actual_date"] = datetime.now().strftime("%Y-%m-%d")

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
    """Record a check-in for a milestone and auto-update status (atomic)."""
    validate_plan_name(plan_name)
    if morale not in VALID_MORALE:
        raise ValueError(f"Invalid morale: {morale}")
    if hours_spent < 0:
        raise ValueError("hours_spent must be >= 0")
    progress_pct = max(0, min(100, progress_pct))

    def _do_checkin(plan):
        if plan is None:
            raise ValueError(f"Plan '{plan_name}' not found")
        milestone = _find_milestone(plan, milestone_id)

        if milestone["status"] == "completed":
            raise ValueError(
                f"Milestone '{milestone_id}' is already completed."
            )
        if progress_pct < milestone.get("completion_pct", 0):
            raise ValueError(
                f"Progress cannot decrease (current: {milestone['completion_pct']}%)"
            )

        checkin = {
            "date": datetime.now(timezone.utc).isoformat(),
            "progress_pct": progress_pct,
            "hours_spent": max(0, hours_spent),
            "notes": notes,
            "blockers": blockers,
            "morale": morale,
        }
        milestone.setdefault("checkins", []).append(checkin)
        milestone["completion_pct"] = progress_pct
        current_actual = milestone.get("effort_hours_actual") or 0
        milestone["effort_hours_actual"] = current_actual + max(0, hours_spent)

        if progress_pct >= 100:
            milestone["status"] = "completed"
            milestone["actual_date"] = datetime.now().strftime("%Y-%m-%d")
        elif progress_pct > 0 and milestone["status"] == "pending":
            milestone["status"] = "in_progress"

        if blockers and milestone["status"] != "completed":
            milestone["status"] = "blocked"
            milestone["notes"] = (
                f"{milestone.get('notes', '')}\n[blocker] {blockers}".strip()
            )
        return plan  # return modified plan

    result_checkin = None
    try:
        plan = modify_plan(plan_name, _do_checkin)
    except ValueError:
        raise
    update_index_entry(plan_name, plan)

    # Extract the just-added checkin for the return value
    milestone = _find_milestone(plan, milestone_id)

    from plan_tracker.daily_tracker import auto_confirm_from_checkin
    auto_confirm_from_checkin(plan_name, progress_pct)

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

    now = datetime.now(timezone.utc)
    result = []

    for m in plan.get("milestones", []):
        if m["status"] in ("completed", "blocked"):
            continue
        target = m.get("target_date", "")
        if not target:
            continue
        try:
            target_dt = datetime.fromisoformat(target).replace(tzinfo=timezone.utc)
            # Treat target_date as end-of-day
            target_dt = target_dt.replace(hour=23, minute=59, second=59)
            diff = (target_dt - now).days
        except (ValueError, TypeError):
            continue

        if diff <= days_ahead:
            result.append({
                "plan_name": plan_name,
                "milestone_id": m["id"],
                "milestone_title": m["title"],
                "status": m["status"],
                "target_date": target,
                "days_remaining": max(diff, 0),
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
        last_dt = datetime.fromisoformat(last["date"]).replace(tzinfo=None)
        return (datetime.now() - last_dt).days > max_days
    except (ValueError, KeyError):
        return False
