"""Notification queue for plan-tracker daemon.

All operations protected by LockedFile (threading.Lock + fcntl.flock).
"""

import fcntl
import json
import logging
import uuid
from datetime import datetime, timezone

from plan_tracker.file_lock import LockedFile
from plan_tracker.storage import DATA_DIR

logger = logging.getLogger("plan_tracker.notification_queue")

QUEUE_FILE = DATA_DIR / "notification_queue.json"
MAX_QUEUE_SIZE = 500


def enqueue(plan_name: str, ntype: str, message: str,
            plan_title: str = "", milestone_title: str = "",
            milestone_id: str = "", channel: str = "mcp") -> str:
    """Add a notification to the queue. Returns the notification ID."""
    with LockedFile(QUEUE_FILE, default={"queue": []}) as q:
        note_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "id": note_id, "created_at": now,
            "plan_name": plan_name, "type": ntype,
            "message": message, "plan_title": plan_title,
            "milestone_title": milestone_title,
            "milestone_id": milestone_id,
            "channel": channel,  # source channel for delivery routing
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


def _read_queue_shared() -> dict:
    """Read queue with shared lock — faster, doesn't block other readers."""
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not QUEUE_FILE.exists():
        return {"queue": []}
    with open(QUEUE_FILE, "r", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return {"queue": []}
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def fetch_all() -> list[dict]:
    """Return all undelivered notifications (shared read lock)."""
    q = _read_queue_shared()
    return [n for n in q["queue"] if not n["delivered"]]


def mark_delivered(ids: list[str]) -> int:
    """Mark notifications as delivered. Returns count of marked items."""
    if not ids:
        return 0
    with LockedFile(QUEUE_FILE, default={"queue": []}) as q:
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
    with LockedFile(QUEUE_FILE, default={"queue": []}) as q:
        before = len(q["queue"])
        q["queue"] = [n for n in q["queue"] if not n["delivered"]]
        return before - len(q["queue"])


def remove_for_plan(plan_name: str) -> int:
    """Remove all queue entries for a plan (called on plan delete)."""
    with LockedFile(QUEUE_FILE, default={"queue": []}) as q:
        before = len(q["queue"])
        q["queue"] = [n for n in q["queue"] if n["plan_name"] != plan_name]
        return before - len(q["queue"])
