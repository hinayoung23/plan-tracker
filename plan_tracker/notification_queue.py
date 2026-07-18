"""Notification queue for plan-tracker daemon.

All operations are protected by fcntl.flock(LOCK_EX) to prevent
concurrent write corruption.
"""

import fcntl
import json
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from plan_tracker.storage import DATA_DIR

logger = logging.getLogger("plan_tracker.notification_queue")

QUEUE_FILE = DATA_DIR / "notification_queue.json"
MAX_QUEUE_SIZE = 500


@contextmanager
def _locked_queue():
    """Exclusive lock over the notification queue file."""
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = open(QUEUE_FILE, "r+")
    except FileNotFoundError:
        f = open(QUEUE_FILE, "w+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        try:
            q = json.load(f)
        except (json.JSONDecodeError, ValueError):
            q = {"queue": []}
        yield q
        f.seek(0)
        f.truncate()
        json.dump(q, f, ensure_ascii=False, indent=2)
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def enqueue(plan_name: str, ntype: str, message: str,
            plan_title: str = "", milestone_title: str = "",
            milestone_id: str = "") -> str:
    """Add a notification to the queue. Returns the notification ID."""
    with _locked_queue() as q:
        note_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "id": note_id, "created_at": now,
            "plan_name": plan_name, "type": ntype,
            "message": message, "plan_title": plan_title,
            "milestone_title": milestone_title,
            "milestone_id": milestone_id,
            "delivered": False, "delivered_at": None,
        }
        q["queue"].append(entry)

        if len(q["queue"]) > MAX_QUEUE_SIZE:
            pending = [n for n in q["queue"] if not n["delivered"]]
            delivered = [n for n in q["queue"] if n["delivered"]]
            excess = len(q["queue"]) - MAX_QUEUE_SIZE
            trim_delivered = min(len(delivered), excess)
            delivered = delivered[trim_delivered:]
            excess -= trim_delivered
            if excess > 0:
                pending = pending[excess:]
            q["queue"] = pending + delivered

        logger.info("Enqueued notification %s type=%s plan=%s",
                    note_id, ntype, plan_name)
        return note_id


def fetch_all() -> list[dict]:
    """Return all undelivered notifications (oldest first)."""
    with _locked_queue() as q:
        return [n for n in q["queue"] if not n["delivered"]]


def mark_delivered(ids: list[str]) -> int:
    """Mark notifications as delivered. Returns count of marked items."""
    if not ids:
        return 0
    with _locked_queue() as q:
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        ids_set = set(ids)
        for note in q["queue"]:
            if note["id"] in ids_set and not note["delivered"]:
                note["delivered"] = True
                note["delivered_at"] = now
                count += 1
        return count


def get_pending_text() -> str:
    """Return pending notifications as human-readable text."""
    pending = fetch_all()
    if not pending:
        return ""
    lines = []
    for note in pending:
        lines.append(f"--- [{note['type']}] {note['plan_title']} ---")
        lines.append(note["message"])
        lines.append(f"(id: {note['id']})")
        lines.append("")
    return "\n".join(lines)


def clear_all() -> int:
    """Remove all delivered notifications. Returns count removed."""
    with _locked_queue() as q:
        before = len(q["queue"])
        q["queue"] = [n for n in q["queue"] if not n["delivered"]]
        return before - len(q["queue"])


def remove_for_plan(plan_name: str) -> int:
    """Remove all queue entries for a plan (called on plan delete)."""
    with _locked_queue() as q:
        before = len(q["queue"])
        q["queue"] = [n for n in q["queue"] if n["plan_name"] != plan_name]
        return before - len(q["queue"])
