"""Scheduled reminder engine.

Uses sched to fire reminders at exact configured times instead of
polling every 5 minutes. On startup, catches up on any reminders
that were missed while the daemon was down.

Notifications are dispatched through configured channels.
"""

import json
import logging
import sched
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plan_tracker.file_lock import LockedFile

from plan_tracker.storage import INDEX_FILE, DATA_DIR, load_plan, load_index
from plan_tracker.notification import EmailChannel, WebhookChannel
from plan_tracker.notification_queue import enqueue as enqueue_notification
from plan_tracker.daily_tracker import (
    get_today_state,
    record_checkin_reminded,
    record_review_reminded,
    check_review_timeout,
    auto_mark_incomplete,
    get_archived_for_date,
    catch_up_past_timeouts,
)

logger = logging.getLogger("plan_tracker.reminder")

NOTIFICATION_COOLDOWN_HOURS = 12
STATE_FILE = DATA_DIR / ".reminder_state.json"
RESCHEDULE_MARKER = DATA_DIR / ".reschedule_needed"


def _locked_state():
    """Backwards-compatible wrapper around LockedFile for reminder state."""
    return LockedFile(STATE_FILE, default={})


def check_now() -> None:
    """Module-level helper: run a one-shot check of all plans.

    Creates a temporary ReminderEngine that runs _check_all and
    immediately stops.  Safe to call from the MCP server process
    without interfering with the running daemon.
    """
    engine = ReminderEngine()
    try:
        engine._check_all()
    finally:
        engine.stop()


def _local_now() -> datetime:
    """Wall-clock time in the system's local timezone."""
    return datetime.now()


def _cooldown_now_iso() -> str:
    """Local-time timestamp for cooldown state storage."""
    return _local_now().isoformat()


class ReminderEngine:

    def __init__(self):
        self._stop = threading.Event()
        self._scheduler = sched.scheduler(_time.time, self._interruptible_sleep)
        self._thread = None

    # ── Interruptible sleep (respects _stop) ──────────────────────

    def _interruptible_sleep(self, delay: float) -> None:
        """Sleep that returns immediately when stop() is called."""
        self._stop.wait(delay)

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # Clean up delivered notifications on startup
        self._cleanup_notification_queue()
        # Catch up on any reminders missed while the daemon was down
        self._check_all()
        # After catch-up, schedule timeout checks for today's reviews
        self._schedule_catchup_timeouts()
        # Schedule future events at exact configured times
        self._schedule_all_events()
        # Schedule daily notification queue cleanup
        self._schedule_event("03:00", self._fire_queue_cleanup, "__system__")
        self._thread = threading.Thread(
            target=self._scheduler.run, daemon=True, name="plan-tracker-reminder",
        )
        self._thread.start()
        # Watch for reschedule requests from MCP server
        self._reschedule_thread = threading.Thread(
            target=self._reschedule_watch, daemon=True,
            name="plan-tracker-reschedule-watch",
        )
        self._reschedule_thread.start()
        logger.info("Reminder engine started (event-scheduled mode)")

    def _reschedule_watch(self) -> None:
        """Periodically check for a reschedule marker file.

        The MCP server touches this file when a plan is created or
        its reminder config changes, so the running scheduler picks
        up new plans without a daemon restart.
        """
        while not self._stop.is_set():
            self._stop.wait(60)
            if self._stop.is_set():
                break
            try:
                if RESCHEDULE_MARKER.exists():
                    RESCHEDULE_MARKER.unlink()
                    logger.info("Reschedule marker detected — reloading events")
                    self._schedule_all_events()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        for event in list(self._scheduler.queue):
            self._scheduler.cancel(event)
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Reminder engine stopped")

    def check_now(self):
        """Manually trigger an immediate check of all plans."""
        self._check_all()

    def schedule_plan(self, plan_name: str) -> None:
        """Re-schedule all events for a single plan immediately.

        Cancels old events for this plan, schedules new ones, and
        wakes the scheduler if a new event is earlier than the next
        queued event.
        """
        self._cancel_plan_events(plan_name)

        plan = load_plan(plan_name)
        if not plan:
            return
        reminders = plan.get("reminders", {})
        if not reminders.get("enabled", True):
            return

        if reminders.get("daily_checkin_enabled", True):
            t = reminders.get("daily_checkin_time", "08:30")
            self._schedule_event(t, self._fire_daily_checkin, plan_name)

        if reminders.get("daily_review_enabled", True):
            t = reminders.get("daily_review_time", "21:30")
            self._schedule_event(t, self._fire_daily_review, plan_name)

        t = reminders.get("daily_checkin_time", "08:30")
        self._schedule_event(t, self._fire_milestone_check, plan_name, offset_minutes=5)

        weekly_day = reminders.get("weekly_checkin_day", "")
        if weekly_day:
            wtime = reminders.get("weekly_checkin_time", "09:00")
            self._schedule_weekly_event(wtime, weekly_day, plan_name)

        # Wake the scheduler so it picks up new earlier events.
        # sched sleeps until the next queued event; inserting an
        # earlier event requires injecting a dummy immediate event.
        self._scheduler.enter(0, 0, lambda: None)


    def _schedule_catchup_timeouts(self) -> None:
        """After startup catch-up, schedule timeout checks for any reviews
        that were just re-sent during the evening window."""
        index = load_index()
        if not index or not index.get("plans"):
            return
        now = _local_now()
        for entry in index["plans"]:
            plan = load_plan(entry["name"])
            if not plan or not plan.get("reminders", {}).get("enabled", True):
                continue
            reminders = plan.get("reminders", {})
            if not reminders.get("daily_review_enabled", True):
                continue
            rev_time = reminders.get("daily_review_time", "21:30")
            timeout_m = reminders.get("confirmation_timeout_minutes", 10)
            if _in_time_window(now, rev_time):
                self._schedule_event(rev_time, self._fire_review_timeout,
                                    entry["name"], offset_minutes=timeout_m)

    def _schedule_weekly_event(self, time_str: str, day_name: str, plan_name: str) -> None:
        """Schedule a weekly check at *time_str* on the next *day_name*."""
        if not _validate_time_str(time_str):
            return
        days_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                    "friday": 4, "saturday": 5, "sunday": 6}
        target_dow = days_map.get(day_name.lower())
        if target_dow is None:
            return
        now = _local_now()
        h, m = map(int, time_str.split(":"))
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        days_ahead = target_dow - now.weekday()
        if days_ahead < 0 or (days_ahead == 0 and target <= now):
            days_ahead += 7
        target += timedelta(days=days_ahead)
        callback = lambda pn: self._check_milestones_for_plan(pn)
        self._scheduler.enterabs(target.timestamp(), 1, callback, (plan_name,))
        logger.debug("Scheduled weekly check for %s at %s", plan_name, target.isoformat())


    # ── Scheduling ────────────────────────────────────────────────

    def _cancel_plan_events(self, plan_name: str) -> None:
        """Remove all scheduled events for a plan."""
        for event in list(self._scheduler.queue):
            if len(event.argument) > 0 and event.argument[0] == plan_name:
                self._scheduler.cancel(event)

    def _schedule_all_events(self) -> None:
        """Schedule the next event of each type for every enabled plan.
        Cancels all existing events first to prevent duplicates."""
        # Cancel all existing events
        for event in list(self._scheduler.queue):
            self._scheduler.cancel(event)
        # Re-schedule queue cleanup
        self._schedule_event("03:00", self._fire_queue_cleanup, "__system__")

        index = load_index()
        if not index or not index.get("plans"):
            return
        for entry in index["plans"]:
            plan = load_plan(entry["name"])
            if not plan:
                continue
            reminders = plan.get("reminders", {})
            if not reminders.get("enabled", True):
                continue

            if reminders.get("daily_checkin_enabled", True):
                t = reminders.get("daily_checkin_time", "08:30")
                self._schedule_event(t, self._fire_daily_checkin, entry["name"])

            if reminders.get("daily_review_enabled", True):
                t = reminders.get("daily_review_time", "21:30")
                self._schedule_event(t, self._fire_daily_review, entry["name"])

            # Milestone checks
            t = reminders.get("daily_checkin_time", "08:30")
            self._schedule_event(t, self._fire_milestone_check, entry["name"],
                                offset_minutes=5)

            # Weekly check
            weekly_day = reminders.get("weekly_checkin_day", "")
            if weekly_day:
                wtime = reminders.get("weekly_checkin_time", "09:00")
                self._schedule_weekly_event(wtime, weekly_day, entry["name"])

    def _schedule_event(self, time_str: str, callback, plan_name: str,
                        offset_minutes: int = 0) -> None:
        """Schedule *callback(plan_name)* at the next occurrence of HH:MM (+ offset)."""
        if not _validate_time_str(time_str):
            logger.warning("Invalid time string '%s' for plan '%s' — skipping", time_str, plan_name)
            return
        try:
            h, m = map(int, time_str.split(":"))
        except (ValueError, IndexError):
            return
        now = _local_now()
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        target += timedelta(minutes=offset_minutes)
        if target <= now:
            target += timedelta(days=1)
        self._scheduler.enterabs(target.timestamp(), 1, callback, (plan_name,))
        logger.debug("Scheduled %s for %s at %s",
                     callback.__name__, plan_name, target.isoformat())

    # ── Event callbacks ───────────────────────────────────────────

    def _fire_daily_checkin(self, plan_name: str) -> None:
        """Fire the morning check-in reminder, then reschedule."""
        plan = load_plan(plan_name)
        if not plan:
            return
        reminders = plan.get("reminders", {})

        # Atomic cooldown check — prevents duplicate notifications when
        # multiple trigger paths (daemon + cron / startup catch-up) race.
        cooldown_key = f"{plan_name}:daily_checkin"
        with _locked_state() as state:
            if not _should_notify(state, cooldown_key, "daily_checkin"):
                logger.debug("Skipping daily checkin for %s — within cooldown", plan_name)
            else:
                # Release lock before slow I/O to avoid contention
                state[cooldown_key] = {"type": "daily_checkin", "time": _cooldown_now_iso()}
        # — lock released here; dispatch outside the lock ——————

        if state.get(cooldown_key, {}).get("time"):
            today_state = get_today_state(plan_name)
            if not today_state.get("checkin_reminded"):
                record_checkin_reminded(plan_name)
                milestone = _get_active_milestone(plan)
                archived = get_archived_for_date(plan_name, _today_str())
                entry = _plan_index_entry(plan_name)
                if entry:
                    self._dispatch([
                        _build_daily_checkin(entry, plan, milestone, archived),
                    ])

        # Reschedule for tomorrow
        if reminders.get("daily_checkin_enabled", True):
            t = reminders.get("daily_checkin_time", "08:30")
            self._schedule_event(t, self._fire_daily_checkin, plan_name)

    def _fire_daily_review(self, plan_name: str) -> None:
        """Fire the evening review reminder, then reschedule."""
        plan = load_plan(plan_name)
        if not plan:
            return
        reminders = plan.get("reminders", {})

        # Atomic cooldown check — see _fire_daily_checkin for rationale.
        cooldown_key = f"{plan_name}:daily_review"
        with _locked_state() as state:
            if not _should_notify(state, cooldown_key, "daily_review"):
                logger.debug("Skipping daily review for %s — within cooldown", plan_name)
            else:
                state[cooldown_key] = {"type": "daily_review", "time": _cooldown_now_iso()}

        if state.get(cooldown_key, {}).get("time"):
            today_state = get_today_state(plan_name)
            if not today_state.get("review_reminded"):
                record_review_reminded(plan_name)
                milestone = _get_active_milestone(plan)
                timeout = reminders.get("confirmation_timeout_minutes", 10)
                entry = _plan_index_entry(plan_name)
                if entry:
                    self._dispatch([
                        _build_daily_review(entry, plan, milestone, timeout),
                    ])

        # Reschedule for tomorrow
        if reminders.get("daily_review_enabled", True):
            t = reminders.get("daily_review_time", "21:30")
            self._schedule_event(t, self._fire_daily_review, plan_name)

        # Also schedule the timeout check
        timeout_minutes = reminders.get("confirmation_timeout_minutes", 10)
        t = reminders.get("daily_review_time", "21:30")
        self._schedule_event(t, self._fire_review_timeout, plan_name,
                            offset_minutes=timeout_minutes)

    def _fire_review_timeout(self, plan_name: str) -> None:
        """Check for review timeout and auto-mark if needed."""
        plan = load_plan(plan_name)
        if not plan:
            return
        reminders = plan.get("reminders", {})
        timeout_minutes = reminders.get("confirmation_timeout_minutes", 10)

        if check_review_timeout(plan_name, timeout_minutes):
            key = f"{plan_name}:daily_timeout"
            with _locked_state() as state:
                if not _should_notify(state, key, "daily_timeout"):
                    return
                state[key] = {"type": "daily_timeout", "time": _cooldown_now_iso()}
            result = auto_mark_incomplete(plan_name)
            if result:
                entry = _plan_index_entry(plan_name)
                if entry:
                    self._dispatch([_build_daily_timeout(entry, plan)])

    def _fire_milestone_check(self, plan_name: str) -> None:
        """Check all milestones for one plan (overdue/upcoming/stale/weekly), then reschedule."""
        self._check_milestones_for_plan(plan_name)
        # Reschedule for tomorrow (5 min after checkin time)
        plan = load_plan(plan_name)
        if plan:
            reminders = plan.get("reminders", {})
            t = reminders.get("daily_checkin_time", "08:30")
            self._schedule_event(t, self._fire_milestone_check, plan_name,
                                offset_minutes=5)

    # ── Full check (used on startup and check_now) ────────────────

    def _check_all(self) -> None:
        """Run a full check of all plans. Safe to call anytime — cooldowns
        prevent duplicates with scheduled events."""
        index = load_index()
        if not index or not index.get("plans"):
            return

        notifications = []
        for entry in index["plans"]:
            plan = load_plan(entry["name"])
            if not plan:
                continue
            reminders = plan.get("reminders", {})
            if not reminders.get("enabled", True):
                continue

            # Daily reminders
            daily = self._check_daily(entry, plan)
            notifications.extend(daily)

            # Milestone checks (inline)
            msgs = self._check_milestones_for_plan(entry["name"])
            # _check_milestones_for_plan dispatches internally, so no need to extend

        if notifications:
            self._dispatch(notifications)

    def _cleanup_notification_queue(self) -> None:
        """Remove delivered notifications and trim old daily state."""
        try:
            from plan_tracker.notification_queue import clear_all
            removed = clear_all()
            if removed > 0:
                logger.info("Cleaned up %d delivered notification(s)", removed)
        except Exception:
            logger.debug("Notification queue cleanup skipped", exc_info=True)

        # Trim daily state entries older than 90 days
        try:
            from plan_tracker.daily_tracker import trim_old_entries
            trimmed = trim_old_entries(retention_days=90)
            if trimmed > 0:
                logger.info("Trimmed %d old daily state entries", trimmed)
        except Exception:
            logger.debug("Daily state trim skipped", exc_info=True)

    def _fire_queue_cleanup(self, _plan_name: str) -> None:
        """Periodic callback to purge delivered notifications."""
        self._cleanup_notification_queue()
        # Reschedule for tomorrow
        self._schedule_event("03:00", self._fire_queue_cleanup, "__system__")

    def _check_milestones_for_plan(self, plan_name: str) -> None:
        """Check milestones for a single plan and dispatch any notifications."""
        plan = load_plan(plan_name)
        if not plan:
            return
        entry = _plan_index_entry(plan_name)
        if not entry:
            return

        reminders = plan.get("reminders", {})
        before_days = reminders.get("before_due_days", 3)
        notifications = []

        with _locked_state() as state:
            for m in plan.get("milestones", []):
                if m["status"] in ("completed", "blocked"):
                    continue
                target = m.get("target_date", "")
                if not target:
                    continue
                target_dt = _parse_date(target)
                if target_dt is None:
                    continue
                days_remaining = (target_dt - _local_now()).days

                if days_remaining < 0:
                    key = _cooldown_key(entry['name'], m['id'], "overdue")
                    if _should_notify(state, key, "overdue"):
                        notifications.append(_build_overdue(m, entry, days_remaining))
                        state[key] = {"type": "overdue", "time": _cooldown_now_iso()}
                elif 0 <= days_remaining <= before_days:
                    key = _cooldown_key(entry['name'], m['id'], "upcoming")
                    if _should_notify(state, key, "upcoming"):
                        notifications.append(_build_upcoming(m, entry, days_remaining))
                        state[key] = {"type": "upcoming", "time": _cooldown_now_iso()}
                if _is_stale(m):
                    key = _cooldown_key(entry['name'], m['id'], "stale")
                    if _should_notify(state, key, "stale"):
                        notifications.append(_build_stale(m, entry))
                        state[key] = {"type": "stale", "time": _cooldown_now_iso()}

            weekly_day = reminders.get("weekly_checkin_day", "")
            if weekly_day and _is_today(weekly_day):
                key = f"{entry['name']}:weekly"
                if _should_notify(state, key, "weekly"):
                    notifications.append(_build_weekly(entry, plan))
                    state[key] = {"type": "weekly", "time": _cooldown_now_iso()}

        if notifications:
            self._dispatch(notifications)

    # ── Dispatch ──────────────────────────────────────────────────

    def _dispatch(self, notifications):
        for note in notifications:
            plan_name = note["plan_name"]
            plan = load_plan(plan_name)
            if not plan:
                continue
            channels = plan.get("reminders", {}).get("notification_channels", ["mcp"])

            mtitle = note.get("milestone_title", "")
            mid = note.get("milestone_id", "")
            plan_title = note.get("plan_title", plan_name)
            ntype = note.get("type", "info")

            # Always enqueue once (the queue is the source of truth).
            # Webhook/email channels serve as real-time wakeup signals
            # for the receiver, not as the delivery mechanism itself.
            enqueue_notification(
                plan_name=plan_name, ntype=ntype,
                message=note["message"], plan_title=plan_title,
                milestone_title=mtitle, milestone_id=mid,
            )

            # Send wakeup signals via configured channels
            for ch in channels:
                if ch == "email":
                    ecfg = plan.get("reminders", {}).get("email", {})
                    if ecfg.get("enabled"):
                        EmailChannel(ecfg).send(note, plan_name, mtitle, mid)
                elif ch == "webhook":
                    wcfg = plan.get("reminders", {}).get("webhook", {})
                    if wcfg.get("url"):
                        WebhookChannel(wcfg).send(note, plan_name, mtitle, mid)

    # ── Daily check (used by _check_all on startup) ───────────────

    def _check_daily(self, entry: dict, plan: dict) -> list[dict]:
        """Check and trigger daily check-in / review reminders for one plan.

        Only fires when the current time is within the configured window AND
        the cooldown period has passed (prevents duplicates on daemon restart).
        """
        reminders = plan.get("reminders", {})
        notifications = []
        now = _local_now()

        # Catch up on past unconfirmed reviews (daemon was down)
        timeout_minutes = reminders.get("confirmation_timeout_minutes", 10)
        past_results = catch_up_past_timeouts(entry["name"], timeout_minutes)
        for result in past_results:
            key = f"{entry['name']}:daily_timeout"
            with _locked_state() as state:
                if _should_notify(state, key, "daily_timeout"):
                    state[key] = {"type": "daily_timeout", "time": _cooldown_now_iso()}
                    notifications.append(_build_daily_timeout(entry, plan))

        # Atomic checkin cooldown: only one path wins the race
        should_checkin = False
        if reminders.get("daily_checkin_enabled", True):
            chk_time = reminders.get("daily_checkin_time", "08:30")
            ck_key = f"{entry['name']}:daily_checkin"
            if _in_time_window(now, chk_time):
                with _locked_state() as state:
                    if _should_notify(state, ck_key, "daily_checkin"):
                        state[ck_key] = {"type": "daily_checkin", "time": _cooldown_now_iso()}
                        should_checkin = True
        if should_checkin:
            today_state = get_today_state(entry["name"])
            if not today_state.get("checkin_reminded"):
                record_checkin_reminded(entry["name"])
                milestone = _get_active_milestone(plan)
                archived = get_archived_for_date(entry["name"], _today_str())
                notifications.append(
                    _build_daily_checkin(entry, plan, milestone, archived)
                )

        # Atomic review cooldown
        should_review = False
        if reminders.get("daily_review_enabled", True):
            rev_time = reminders.get("daily_review_time", "21:30")
            rv_key = f"{entry['name']}:daily_review"
            if _in_time_window(now, rev_time):
                with _locked_state() as state:
                    if _should_notify(state, rv_key, "daily_review"):
                        state[rv_key] = {"type": "daily_review", "time": _cooldown_now_iso()}
                        should_review = True
        if should_review:
            today_state = get_today_state(entry["name"])
            if not today_state.get("review_reminded"):
                record_review_reminded(entry["name"])
                milestone = _get_active_milestone(plan)
                timeout = reminders.get("confirmation_timeout_minutes", 10)
                notifications.append(
                    _build_daily_review(entry, plan, milestone, timeout)
                )

        # Timeout notification (also atomic)
        if check_review_timeout(entry["name"], timeout_minutes):
            key = f"{entry['name']}:daily_timeout"
            with _locked_state() as state:
                if _should_notify(state, key, "daily_timeout"):
                    state[key] = {"type": "daily_timeout", "time": _cooldown_now_iso()}
            result = auto_mark_incomplete(entry["name"])
            if result:
                notifications.append(_build_daily_timeout(entry, plan))

        return notifications


# ── Helpers ───────────────────────────────────────────────────────

def _plan_index_entry(plan_name: str) -> dict | None:
    """Get the index entry for a plan."""
    index = load_index()
    for p in index.get("plans", []):
        if p["name"] == plan_name:
            return p
    return None


def _get_active_milestone(plan: dict) -> dict | None:
    for m in plan.get("milestones", []):
        if m["status"] == "in_progress":
            return m
    for m in plan.get("milestones", []):
        if m["status"] == "pending":
            return m
    return None


def _today_str() -> str:
    return _local_now().strftime("%Y-%m-%d")


def _in_time_window(now: datetime, time_str: str, window_hours: int = 3) -> bool:
    """Check if *now* is within [time_str, time_str + window_hours].

    Handles cross-midnight windows correctly (e.g. 23:00 + 3h wraps to 02:00).
    """
    try:
        parts = time_str.split(":")
        target_h, target_m = int(parts[0]), int(parts[1])
        target_minutes = target_h * 60 + target_m
        now_minutes = now.hour * 60 + now.minute
        window_minutes = window_hours * 60

        end_minutes = target_minutes + window_minutes
        if end_minutes < 1440:
            # Window within same day
            return target_minutes <= now_minutes < end_minutes
        else:
            # Window wraps past midnight
            end_minutes -= 1440
            return now_minutes >= target_minutes or now_minutes < end_minutes
    except (ValueError, IndexError):
        return False


def _save_state_after_timeout(plan_name: str, ntype: str) -> None:
    state = _load_state()
    key = f"{plan_name}:daily_timeout"
    state[key] = {"type": ntype, "time": _cooldown_now_iso()}
    _save_state(state)


# ── Notification builders ─────────────────────────────────────────

def _build_daily_checkin(entry: dict, plan: dict, milestone: dict | None,
                         archived: dict | None) -> dict:
    today = _today_str()
    plan_title = entry.get("title", entry["name"])
    goal = plan.get("goal", "")
    target_end = entry.get("target_end_date", "")

    msg_parts = [
        f"☀ 早上好！今天是 {today}，开始新的一天吧！",
        f"",
        f"计划：{plan_title}",
        f"目标：{goal}",
        f"目标完成日期：{target_end}",
    ]

    if milestone:
        msg_parts.append(f"")
        msg_parts.append(f"当前里程碑：「{milestone['title']}」")
        msg_parts.append(f"进度：{milestone.get('completion_pct', 0)}%")
        msg_parts.append(f"目标日期：{milestone.get('target_date', '')}")
        msg_parts.append(f"预计工时：{milestone.get('effort_hours_estimate', 0)}h")

    if archived:
        msg_parts.append(f"")
        msg_parts.append(f"📋 昨日补确认（来自 {archived.get('from_date', '')}）：")
        msg_parts.append(f"   状态：{archived.get('completion_status', '')}")
        if archived.get("notes"):
            msg_parts.append(f"   备注：{archived['notes']}")

    msg_parts.append(f"")
    msg_parts.append(f"准备好了吗？开始今天的打卡吧！")

    return {
        "plan_name": entry["name"],
        "milestone_id": milestone["id"] if milestone else "",
        "milestone_title": milestone["title"] if milestone else "",
        "message": "\n".join(msg_parts),
        "type": "daily_checkin",
        "plan_title": plan_title,
        "goal": goal,
        "milestone": milestone,
        "archived_confirmation": archived,
    }


def _build_daily_review(entry: dict, plan: dict, milestone: dict | None,
                        timeout_minutes: int) -> dict:
    plan_title = entry.get("title", entry["name"])

    msg_parts = [
        f"🌙 晚上好！今天计划执行得如何？",
        f"",
        f"计划：{plan_title}",
    ]

    if milestone:
        progress = milestone.get("completion_pct", 0)
        msg_parts.append(f"当前里程碑：「{milestone['title']}」（进度 {progress}%）")

    msg_parts.append(f"")
    msg_parts.append(f"请确认今天的完成情况：")
    msg_parts.append(f"  ✅ 已完成 (completed)")
    msg_parts.append(f"  📌 部分完成 (partial)")
    msg_parts.append(f"  ❌ 未完成 (incomplete)")
    msg_parts.append(f"")
    msg_parts.append(f"⏰ 请在 {timeout_minutes} 分钟内回复，超时将自动标记为未完成。")

    return {
        "plan_name": entry["name"],
        "milestone_id": milestone["id"] if milestone else "",
        "milestone_title": milestone["title"] if milestone else "",
        "message": "\n".join(msg_parts),
        "type": "daily_review",
        "plan_title": plan_title,
        "goal": plan.get("goal", ""),
        "milestone": milestone,
        "timeout_minutes": timeout_minutes,
    }


def _build_daily_timeout(entry: dict, plan: dict) -> dict:
    plan_title = entry.get("title", entry["name"])

    msg_parts = [
        f"⏰ 超时通知",
        f"",
        f"计划「{plan_title}」的晚间确认已超时，系统已自动将今天的计划标记为「未完成」。",
        f"",
        f"如需补确认，请使用 daily_confirm 工具，确认将归档到明天的记录中。",
    ]

    return {
        "plan_name": entry["name"],
        "milestone_id": "",
        "milestone_title": "",
        "message": "\n".join(msg_parts),
        "type": "daily_timeout",
        "plan_title": plan_title,
    }


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


# ── Utils ─────────────────────────────────────────────────────────

def _parse_date(date_str):
    """Parse a date string, treating date-only values as end-of-day."""
    try:
        dt = datetime.fromisoformat(date_str)
        # If no time component (date only like "2026-12-31"), use end-of-day
        if len(date_str) == 10 and "T" not in date_str:
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt
    except (ValueError, TypeError):
        return None


def _validate_time_str(time_str: str) -> bool:
    """Check that a time string is valid HH:MM."""
    try:
        h, m = map(int, time_str.split(":"))
        return 0 <= h <= 23 and 0 <= m <= 59
    except (ValueError, IndexError):
        return False


def _is_stale(m):
    checkins = m.get("checkins", [])
    if not checkins:
        return False
    try:
        last_dt = datetime.fromisoformat(checkins[-1]["date"]).replace(tzinfo=None)
        return (_local_now() - last_dt).days > 7
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
    return _local_now().weekday() == expected


def _should_notify(state, key, ntype):
    if key not in state:
        return True
    last = state[key]
    if last.get("type") != ntype:
        return True
    try:
        # Cooldown timestamps are stored in local time
        last_dt = datetime.fromisoformat(last["time"])
        hours = (_local_now() - last_dt).total_seconds() / 3600
        return hours >= NOTIFICATION_COOLDOWN_HOURS
    except (ValueError, KeyError, TypeError):
        return True


def _cooldown_key(plan_name: str, milestone_id: str, ntype: str) -> str:
    """Build a cooldown key that includes the notification type.

    This prevents type-override races where an overdue notification
    overwrites the cooldown for an upcoming check on the same milestone.
    """
    if milestone_id:
        return f"{plan_name}:{milestone_id}:{ntype}"
    return f"{plan_name}:{ntype}"


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


def remove_plan_state(plan_name: str) -> None:
    """Remove all cooldown state entries for a plan (called on plan delete)."""
    try:
        with _locked_state() as state:
            prefix = f"{plan_name}:"
            keys_to_remove = [k for k in state if k.startswith(prefix)]
            for k in keys_to_remove:
                del state[k]
    except Exception:
        pass
