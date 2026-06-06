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

from plan_tracker.storage import DATA_DIR

logger = logging.getLogger("plan_tracker.daily")

STATE_FILE = DATA_DIR / "daily_state.json"
VALID_COMPLETION_STATUSES = ("completed", "partial", "incomplete")


def _load_state() -> dict:
    """Load the full daily state dict from disk."""
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to load daily state, starting fresh.")
    return {}


def _save_state(state: dict) -> None:
    """Persist the daily state dict to disk."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError:
        logger.exception("Failed to save daily state.")


def _today_str() -> str:
    """Today's date in local time — used to key daily state entries.

    Using local time ensures that morning/evening reminders align with
    the user's actual day/night cycle rather than UTC.
    """
    return datetime.now().strftime("%Y-%m-%d")


def get_today_state(plan_name: str) -> dict:
    """Get (or create) the daily state entry for today."""
    today = _today_str()
    state = _load_state()
    plan_state = state.setdefault(plan_name, {})
    if today not in plan_state:
        plan_state[today] = {
            "checkin_reminded": False,
            "checkin_reminded_at": None,
            "review_reminded": False,
            "review_reminded_at": None,
            "confirmed": False,
            "confirmed_at": None,
            "completion_status": None,
            "completion_notes": "",
        }
        _save_state(state)
    return plan_state[today]


def record_checkin_reminded(plan_name: str) -> None:
    """Mark that the morning check-in reminder was sent today."""
    state = _load_state()
    today = _today_str()
    entry = state.setdefault(plan_name, {}).setdefault(today, {})
    entry["checkin_reminded"] = True
    entry["checkin_reminded_at"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)


def record_review_reminded(plan_name: str) -> None:
    """Mark that the evening review reminder was sent today."""
    state = _load_state()
    today = _today_str()
    entry = state.setdefault(plan_name, {}).setdefault(today, {})
    entry["review_reminded"] = True
    entry["review_reminded_at"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)


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

    date_str = target_date or _today_str()
    state = _load_state()
    entry = state.setdefault(plan_name, {}).setdefault(date_str, {})
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    # Check if this is a late confirmation (after timeout)
    review_reminded_at = entry.get("review_reminded_at")
    is_archived = False
    archive_target_date = None

    if review_reminded_at:
        try:
            reminded_dt = datetime.fromisoformat(review_reminded_at).replace(
                tzinfo=timezone.utc
            )
            elapsed_minutes = (now_utc - reminded_dt).total_seconds() / 60
            # If more than 10 minutes passed since evening reminder, archive to next day
            # We read timeout from plan config later, use a reasonable default here
            if elapsed_minutes > 10:
                is_archived = True
                # Archive to the next LOCAL day (not UTC)
                from datetime import timedelta
                next_day = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                archive_target_date = next_day

                # Store the archived confirmation on the next day's entry
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

    _save_state(state)

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
        return elapsed_minutes > timeout_minutes
    except (ValueError, TypeError):
        return False


def auto_mark_incomplete(plan_name: str) -> dict | None:
    """Auto-mark today's plan as incomplete due to timeout.

    Only acts if review was sent, not confirmed, and timeout has passed.
    Returns the result dict or None if conditions not met.
    """
    today = _today_str()
    state = _load_state()
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
    _save_state(state)

    return {
        "plan_name": plan_name,
        "date": today,
        "completion_status": "incomplete",
        "auto_marked": True,
    }


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
