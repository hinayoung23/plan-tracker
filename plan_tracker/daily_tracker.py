"""Daily check-in and review state management.

Tracks per-plan, per-date state for the two daily reminder types:
  - Morning check-in reminder (daily_checkin)
  - Evening review confirmation (daily_review)

State is persisted to data/daily_state.json.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from plan_tracker.file_lock import LockedFile
from plan_tracker.storage import DATA_DIR

logger = logging.getLogger("plan_tracker.daily")

STATE_FILE = DATA_DIR / "daily_state.json"
VALID_COMPLETION_STATUSES = ("completed", "partial", "incomplete")


def _load_state() -> dict:
    """Load the full daily state dict from disk (read-only helper)."""
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to load daily state, starting fresh.")
    return {}


def _locked_daily_state():
    """Locked read-modify-write context manager for daily state."""
    return LockedFile(STATE_FILE, default={})


def _today_str() -> str:
    """Today's date in local time — used to key daily state entries.

    Using local time ensures that morning/evening reminders align with
    the user's actual day/night cycle rather than UTC.
    """
    return datetime.now().strftime("%Y-%m-%d")


def get_today_state(plan_name: str) -> dict:
    """Get (or create) the daily state entry for today."""
    today = _today_str()
    # Read-only fast path: check if entry already exists
    state = _load_state()
    if plan_name in state and today in state[plan_name]:
        return state[plan_name][today]
    # Create entry under lock
    with _locked_daily_state() as state:
        entry = state.setdefault(plan_name, {}).setdefault(today, {
            "date": today,
            "checkin_reminded": False,
            "checkin_reminded_at": None,
            "review_reminded": False,
            "review_reminded_at": None,
            "confirmed": False,
            "confirmed_at": None,
            "completion_status": None,
            "completion_notes": "",
        })
    return entry


def record_checkin_reminded(plan_name: str) -> None:
    """Mark that the morning check-in reminder was sent today."""
    today = _today_str()
    now_iso = datetime.now(timezone.utc).isoformat()
    with _locked_daily_state() as state:
        entry = state.setdefault(plan_name, {}).setdefault(today, {})
        entry["checkin_reminded"] = True
        entry["checkin_reminded_at"] = now_iso


def record_review_reminded(plan_name: str) -> None:
    """Mark that the evening review reminder was sent today."""
    today = _today_str()
    now_iso = datetime.now(timezone.utc).isoformat()
    with _locked_daily_state() as state:
        entry = state.setdefault(plan_name, {}).setdefault(today, {})
        entry["review_reminded"] = True
        entry["review_reminded_at"] = now_iso


def record_confirmation(
    plan_name: str,
    completion_status: str,
    notes: str = "",
    target_date: str | None = None,
) -> dict:
    """Record a user confirmation of today's plan completion.

    Args:
        plan_name: The plan name.
        completion_status: One of completed, partial, incomplete.
        notes: Optional notes from the user.
        target_date: The date this confirmation applies to (default: today).

    Returns a dict with the result and any archive information.
    """
    if completion_status not in VALID_COMPLETION_STATUSES:
        raise ValueError(
            f"Invalid completion status: {completion_status}. "
            f"Must be one of: {VALID_COMPLETION_STATUSES}"
        )

    # Validate plan exists
    from plan_tracker.storage import load_plan, validate_plan_name
    validate_plan_name(plan_name)
    if load_plan(plan_name) is None:
        raise ValueError(f"Plan '{plan_name}' not found")

    date_str = target_date or _today_str()
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    timeout_minutes = 10
    try:
        plan = load_plan(plan_name)
        if plan:
            timeout_minutes = plan.get("reminders", {}).get(
                "confirmation_timeout_minutes", 10
            )
    except Exception:
        pass

    is_archived = False
    archive_target_date = None

    with _locked_daily_state() as state:
        entry = state.setdefault(plan_name, {}).setdefault(date_str, {})
        review_reminded_at = entry.get("review_reminded_at")

        if review_reminded_at:
            try:
                reminded_dt = datetime.fromisoformat(review_reminded_at).replace(
                    tzinfo=timezone.utc
                )
                elapsed_minutes = (now_utc - reminded_dt).total_seconds() / 60
                if elapsed_minutes > timeout_minutes:
                    is_archived = True
                    from datetime import timedelta
                    next_day = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                    archive_target_date = next_day
                    next_entry = state.setdefault(plan_name, {}).setdefault(next_day, {})
                    next_entry["archived_confirmation"] = {
                        "from_date": date_str,
                        "completion_status": completion_status,
                        "notes": notes,
                        "confirmed_at": now_iso,
                    }
            except (ValueError, TypeError):
                pass

        if not is_archived:
            entry["confirmed"] = True
            entry["confirmed_at"] = now_iso
            entry["completion_status"] = completion_status
            entry["completion_notes"] = notes

    return {
        "plan_name": plan_name,
        "date": date_str,
        "completion_status": completion_status,
        "notes": notes,
        "confirmed_at": now_iso,
        "is_archived": is_archived,
        "archive_target_date": archive_target_date,
    }


def check_review_timeout(plan_name: str, timeout_minutes: int = 10) -> bool:
    """Check if the evening review has timed out without confirmation.

    Returns True if:
    - Evening review was sent today
    - User hasn't confirmed
    - More than timeout_minutes have passed since the reminder
    """
    today = _today_str()
    state = _load_state()
    entry = state.get(plan_name, {}).get(today, {})

    if not entry.get("review_reminded"):
        return False
    if entry.get("confirmed"):
        return False

    review_reminded_at = entry.get("review_reminded_at")
    if not review_reminded_at:
        return False

    try:
        reminded_dt = datetime.fromisoformat(review_reminded_at).replace(
            tzinfo=timezone.utc
        )
        elapsed_minutes = (datetime.now(timezone.utc) - reminded_dt).total_seconds() / 60
        return elapsed_minutes >= timeout_minutes
    except (ValueError, TypeError):
        return False


def auto_mark_incomplete(plan_name: str) -> dict | None:
    """Auto-mark today's plan as incomplete due to timeout."""
    today = _today_str()
    with _locked_daily_state() as state:
        entry = state.get(plan_name, {}).get(today, {})
        if not entry.get("review_reminded"):
            return None
        if entry.get("confirmed"):
            return None
        entry["confirmed"] = True
        entry["confirmed_at"] = datetime.now(timezone.utc).isoformat()
        entry["completion_status"] = "incomplete"
        entry["completion_notes"] = "[自动判定] 超时未确认，标记为未完成"
        entry["auto_marked"] = True
        result = {
            "plan_name": plan_name,
            "date": today,
            "completion_status": "incomplete",
            "auto_marked": True,
        }
    return result


def catch_up_past_timeouts(plan_name: str, timeout_minutes: int = 10) -> list[dict]:
    """Auto-mark past days that have unconfirmed reviews.

    When the daemon restarts, this catches up on days where the evening
    review was sent but no confirmation (manual or auto) was recorded.
    Returns a list of auto-marked results (empty if nothing to do).
    """
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    today = _today_str()
    now_utc = datetime.now(timezone.utc)
    results = []

    with _locked_daily_state() as state:
        plan_state = state.get(plan_name, {})
        for date_str in sorted(plan_state.keys()):
            if date_str >= today:
                continue
            if date_str < cutoff:
                continue
            entry = plan_state[date_str]
            if entry.get("confirmed"):
                continue
            if not entry.get("review_reminded"):
                continue
            review_reminded_at = entry.get("review_reminded_at")
            if not review_reminded_at:
                continue
            try:
                reminded_dt = datetime.fromisoformat(review_reminded_at).replace(
                    tzinfo=timezone.utc
                )
                elapsed_minutes = (now_utc - reminded_dt).total_seconds() / 60
                if elapsed_minutes >= timeout_minutes:
                    entry["confirmed"] = True
                    entry["confirmed_at"] = now_utc.isoformat()
                    entry["completion_status"] = "incomplete"
                    entry["completion_notes"] = (
                        f"[自动判定·补检] {date_str} 晚间确认超时未响应，自动标记为未完成"
                    )
                    entry["auto_marked"] = True
                    results.append({
                        "plan_name": plan_name,
                        "date": date_str,
                        "completion_status": "incomplete",
                        "auto_marked": True,
                        "catch_up": True,
                    })
            except (ValueError, TypeError):
                continue

    if results:
        logger.info(
            "Catch-up: auto-marked %d past unconfirmed day(s) for %s",
            len(results), plan_name,
        )

    return results


def get_archived_for_date(plan_name: str, date_str: str) -> dict | None:
    """Get archived (late) confirmation info for a specific date.

    Used to include yesterday's late confirmation in today's morning reminder.
    """
    state = _load_state()
    entry = state.get(plan_name, {}).get(date_str, {})
    archived = entry.get("archived_confirmation")
    if archived:
        return {
            "plan_name": plan_name,
            "date": date_str,
            **archived,
        }
    return None


def auto_confirm_from_checkin(plan_name: str, progress_pct: int = 0) -> bool:
    """Auto-confirm the evening review when a check-in is recorded during the
    confirmation window.

    Called by ``add_checkin`` after a check-in is successfully written.
    If today's evening review has been sent but not yet confirmed, this
    marks it as confirmed so the daemon does not send a spurious
    "incomplete" timeout notification.

    Returns True if an auto-confirmation was performed, False otherwise.
    """
    today = _today_str()
    with _locked_daily_state() as state:
        entry = state.get(plan_name, {}).get(today, {})

        if not entry.get("review_reminded"):
            return False
        if entry.get("confirmed"):
            return False

        from plan_tracker.storage import load_plan
        plan = load_plan(plan_name)
        if plan:
            timeout_minutes = plan.get("reminders", {}).get("confirmation_timeout_minutes", 10)
            review_reminded_at = entry.get("review_reminded_at")
            if review_reminded_at:
                try:
                    reminded_dt = datetime.fromisoformat(review_reminded_at).replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - reminded_dt).total_seconds() / 60
                    if elapsed > timeout_minutes:
                        return False
                except (ValueError, TypeError):
                    pass

        if progress_pct >= 100:
            completion_status = "completed"
        elif progress_pct > 0:
            completion_status = "partial"
        else:
            completion_status = "partial"

        now_iso = datetime.now(timezone.utc).isoformat()
        notes = "[自动确认] 用户在对话中通过 check-in 更新了进度"

        entry["confirmed"] = True
        entry["confirmed_at"] = now_iso
        entry["completion_status"] = completion_status
        entry["completion_notes"] = notes
        entry["auto_confirmed_from_checkin"] = True

    logger.info(
        "Auto-confirmed review for %s via check-in (status=%s, progress=%d%%)",
        plan_name, completion_status, progress_pct,
    )
    return True


def remove_for_plan(plan_name: str) -> int:
    """Remove all daily state entries for a plan (called on plan delete)."""
    with _locked_daily_state() as state:
        if plan_name in state:
            del state[plan_name]
            return 1
    return 0


def trim_old_entries(retention_days: int = 90) -> int:
    """Remove daily state entries older than *retention_days*."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    trimmed = 0
    with _locked_daily_state() as state:
        for plan_name in list(state.keys()):
            plan_dates = state[plan_name]
            for date_str in list(plan_dates.keys()):
                if date_str < cutoff:
                    del plan_dates[date_str]
                    trimmed += 1
            if not plan_dates:
                del state[plan_name]
    return trimmed
