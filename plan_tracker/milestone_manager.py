"""Milestone lifecycle management and check-in operations."""

from datetime import datetime, timezone

from plan_tracker.storage import load_plan, save_plan, modify_plan_and_index, update_index_entry, validate_plan_name

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
    """Add a new milestone to a plan (atomic)."""
    validate_plan_name(plan_name)
    if not milestone.get("title"):
        raise ValueError("Milestone must have a title")
    if not milestone.get("target_date"):
        raise ValueError("Milestone must have a target_date")
    effort = milestone.get("effort_hours_estimate", 0)
    if not isinstance(effort, (int, float)) or effort < 0:
        raise ValueError("effort_hours_estimate must be >= 0")
    if milestone.get("status", "pending") not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {milestone['status']}")

    result = [None]

    def _add(plan):
        milestones = plan.setdefault("milestones", [])
        insert_at = len(milestones)
        if after_milestone_id:
            found = False
            for i, m in enumerate(milestones):
                if m["id"] == after_milestone_id:
                    insert_at = i + 1; found = True; break
            if not found:
                raise ValueError(f"Milestone '{after_milestone_id}' not found")

        new_ms = dict(milestone)
        new_ms["id"] = _next_milestone_id(milestones)
        new_ms.setdefault("order", insert_at + 1)
        new_ms.setdefault("status", "pending")
        new_ms.setdefault("description", "")
        new_ms.setdefault("actual_date", None)
        new_ms.setdefault("completion_pct", 0)
        new_ms.setdefault("effort_hours_estimate", int(effort))
        new_ms.setdefault("effort_hours_actual", None)
        new_ms.setdefault("notes", "")
        new_ms.setdefault("checkins", [])

        milestones.insert(insert_at, new_ms)
        for i, m in enumerate(milestones):
            m["order"] = i + 1
        result[0] = new_ms

    plan = modify_plan_and_index(plan_name, _add)
    return result[0]


def update_milestone(plan_name: str, milestone_id: str, updates: dict) -> dict:
    """Update milestone fields (atomic).

    When status is changed to ``completed``, syncs completion_pct to
    100 and actual_date to today.  When reopened from completed, clears
    stale completion data so the milestone can start fresh.
    """
    validate_plan_name(plan_name)
    for key in updates:
        if key == "status" and updates[key] not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {updates[key]}")
        if key == "effort_hours_estimate" and updates[key] < 0:
            raise ValueError("effort_hours_estimate must be >= 0")

    result = [None]

    def _update(plan):
        ms = _find_milestone(plan, milestone_id)
        old_status = ms["status"]
        updatable = ("title", "description", "status", "target_date",
                    "effort_hours_estimate", "notes")
        for key in updatable:
            if key in updates:
                ms[key] = updates[key]

        if ms["status"] == "completed":
            if ms.get("completion_pct", 0) < 100:
                ms["completion_pct"] = 100
            if not ms.get("actual_date"):
                ms["actual_date"] = datetime.now().strftime("%Y-%m-%d")

        if old_status == "completed" and ms["status"] != "completed":
            ms["completion_pct"] = 0
            ms["actual_date"] = None
            ms["effort_hours_actual"] = 0

        result[0] = ms

    plan = modify_plan_and_index(plan_name, _update)
    return result[0]


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

    result_milestone = [None]

    def _do_checkin(plan):
        milestone = _find_milestone(plan, milestone_id)
        if milestone["status"] == "completed":
            raise ValueError(f"Milestone '{milestone_id}' is already completed.")
        if progress_pct < milestone.get("completion_pct", 0):
            raise ValueError(f"Progress cannot decrease (current: {milestone['completion_pct']}%)")

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
        result_milestone[0] = milestone

    plan = modify_plan_and_index(plan_name, _do_checkin)
    milestone = result_milestone[0]

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
