"""Scheduled reminder engine.

Runs a background thread that periodically checks all plans for:
- Overdue milestones (past target date)
- Upcoming milestones (within before_due_days)
- Stale check-ins (no update in 7+ days)
- Weekly check-in prompts

Notifications are dispatched through configured channels.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from storage import INDEX_FILE, load_plan, load_index
from notification import McpChannel, EmailChannel

logger = logging.getLogger("plan_tracker.reminder")

CHECK_INTERVAL = 300
NOTIFICATION_COOLDOWN_HOURS = 12
STATE_FILE = Path.home() / "mcp-servers" / "plan-tracker" / "data" / ".reminder_state.json"


class ReminderEngine:

    def __init__(self):
        self._thread = None
        self._stop = threading.Event()
        self._mcp = McpChannel()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="plan-tracker-reminder")
        self._thread.start()
        logger.info("Reminder engine started (check interval: %ds)", CHECK_INTERVAL)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Reminder engine stopped")

    def _run(self):
        while not self._stop.is_set():
            try:
                self._check_all()
            except Exception:
                logger.exception("Reminder check failed")
            self._stop.wait(CHECK_INTERVAL)

    def _check_all(self):
        state = _load_state()
        index = load_index()
        if not index or not index.get("plans"):
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        notifications = []

        for entry in index["plans"]:
            plan = load_plan(entry["name"])
            if not plan:
                continue

            reminders = plan.get("reminders", {})
            if not reminders.get("enabled", True):
                continue

            before_days = reminders.get("before_due_days", 3)

            for m in plan.get("milestones", []):
                if m["status"] in ("completed", "blocked"):
                    continue

                target = m.get("target_date", "")
                if not target:
                    continue

                target_dt = _parse_date(target)
                if target_dt is None:
                    continue

                days_remaining = (target_dt - datetime.now(timezone.utc)).days
                key = f"{entry['name']}:{m['id']}"

                if days_remaining < 0:
                    if _should_notify(state, key, "overdue"):
                        notifications.append(_build_overdue(m, entry, days_remaining))
                        state[key] = {"type": "overdue", "time": _now_iso()}

                elif 0 <= days_remaining <= before_days:
                    if _should_notify(state, key, "upcoming"):
                        notifications.append(_build_upcoming(m, entry, days_remaining))
                        state[key] = {"type": "upcoming", "time": _now_iso()}

                if _is_stale(m) and _should_notify(state, key, "stale"):
                    notifications.append(_build_stale(m, entry))
                    state[key] = {"type": "stale", "time": _now_iso()}

            weekly_day = reminders.get("weekly_checkin_day", "")
            if weekly_day and _is_today(weekly_day):
                key = f"{entry['name']}:weekly"
                if _should_notify(state, key, "weekly"):
                    notifications.append(_build_weekly(entry, plan))
                    state[key] = {"type": "weekly", "time": _now_iso()}

        if notifications:
            self._dispatch(notifications)

        _save_state(state)

    def _dispatch(self, notifications):
        for note in notifications:
            plan_name = note["plan_name"]
            plan = load_plan(plan_name)
            if not plan:
                continue
            channels = plan.get("reminders", {}).get("notification_channels", ["mcp"])

            mtitle = note.get("milestone_title", "")
            mid = note.get("milestone_id", "")

            for ch in channels:
                if ch == "mcp":
                    self._mcp.send(note["message"], plan_name, mtitle, mid)
                elif ch == "email":
                    ecfg = plan.get("reminders", {}).get("email", {})
                    if ecfg.get("enabled"):
                        EmailChannel(ecfg).send(note, plan_name, mtitle, mid)

    def check_now(self):
        self._check_all()
        return []


# ── builders ──

def _build_overdue(m, entry, days):
    ago = abs(days)
    return {
        "plan_name": entry["name"],
        "milestone_id": m["id"],
        "milestone_title": m["title"],
        "message": (
            f"提醒：计划「{entry['title']}」中的里程碑「{m['title']}」已过期 {ago} 天"
            f"（目标日期：{m.get('target_date', '')}），当前进度 {m.get('completion_pct', 0)}%。"
        ),
        "type": "overdue",
        "plan_title": entry["title"],
        "target_date": m.get("target_date", ""),
        "progress_pct": m.get("completion_pct", 0),
        "days_overdue": ago,
    }


def _build_upcoming(m, entry, days):
    return {
        "plan_name": entry["name"],
        "milestone_id": m["id"],
        "milestone_title": m["title"],
        "message": (
            f"提醒：计划「{entry['title']}」中的里程碑「{m['title']}」将在 {days} 天后到期"
            f"（{m.get('target_date', '')}），当前进度 {m.get('completion_pct', 0)}%。"
        ),
        "type": "upcoming",
        "plan_title": entry["title"],
        "target_date": m.get("target_date", ""),
        "progress_pct": m.get("completion_pct", 0),
        "days_remaining": days,
    }


def _build_stale(m, entry):
    return {
        "plan_name": entry["name"],
        "milestone_id": m["id"],
        "milestone_title": m["title"],
        "message": (
            f"提醒：计划「{entry['title']}」中的里程碑「{m['title']}」已超过 7 天"
            f"没有更新进度（当前进度 {m.get('completion_pct', 0)}%）。"
        ),
        "type": "stale",
        "plan_title": entry["title"],
        "progress_pct": m.get("completion_pct", 0),
    }


def _build_weekly(entry, plan):
    milestones = plan.get("milestones", [])
    completed = sum(1 for m in milestones if m["status"] == "completed")
    total = len(milestones)
    return {
        "plan_name": entry["name"],
        "milestone_id": "",
        "milestone_title": "",
        "message": (
            f"每周提醒：计划「{entry['title']}」总体进度 {entry.get('overall_progress_pct', 0)}%，"
            f"目标日期 {entry.get('target_end_date', '')}。新的一周，有什么进展？"
        ),
        "type": "weekly",
        "plan_title": entry["title"],
        "goal": plan.get("goal", ""),
        "progress_pct": entry.get("overall_progress_pct", 0),
        "completed": completed,
        "total": total,
        "target_end_date": entry.get("target_end_date", ""),
    }


# ── utils ──

def _parse_date(date_str):
    try:
        return datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _is_stale(m):
    checkins = m.get("checkins", [])
    if not checkins:
        return False
    try:
        last_dt = datetime.fromisoformat(checkins[-1]["date"]).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_dt).days > 7
    except (ValueError, KeyError):
        return False


def _is_today(day_name):
    days_map = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    expected = days_map.get(day_name.lower())
    if expected is None:
        return False
    return datetime.now(timezone.utc).weekday() == expected


def _should_notify(state, key, ntype):
    if key not in state:
        return True
    last = state[key]
    if last.get("type") != ntype:
        return True
    try:
        last_dt = datetime.fromisoformat(last["time"]).replace(tzinfo=timezone.utc)
        hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        return hours >= NOTIFICATION_COOLDOWN_HOURS
    except (ValueError, KeyError, TypeError):
        return True


def _load_state():
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_state(state):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError:
        pass
