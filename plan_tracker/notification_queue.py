"""Notification queue for plan-tracker daemon.

The daemon writes notifications here; MCP tools and CLI read from here.
This decouples the reminder engine from the delivery mechanism.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from plan_tracker.storage import DATA_DIR

logger = logging.getLogger("plan_tracker.notification_queue")

QUEUE_FILE = DATA_DIR / "notification_queue.json"
MAX_QUEUE_SIZE = 200


def _load_queue() -> dict:
    """Load the full queue from disk."""
    try:
        if QUEUE_FILE.exists():
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to load notification queue, starting fresh.")
    return {"queue": []}


def _save_queue(q: dict) -> None:
    """Persist the queue to disk."""
    try:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(q, f, ensure_ascii=False, indent=2)
    except OSError:
        logger.exception("Failed to save notification queue.")


def enqueue(plan_name: str, ntype: str, message: str,
            plan_title: str = "", milestone_title: str = "",
            milestone_id: str = "") -> str:
    """Add a notification to the queue. Returns the notification ID."""
    q = _load_queue()
    note_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()

    entry = {
        "id": note_id,
        "created_at": now,
        "plan_name": plan_name,
        "type": ntype,
        "message": message,
        "plan_title": plan_title,
        "milestone_title": milestone_title,
        "milestone_id": milestone_id,
        "delivered": False,
        "delivered_at": None,
    }
    q["queue"].append(entry)

    # Trim old delivered entries if queue grows too large
    if len(q["queue"]) > MAX_QUEUE_SIZE:
        delivered = [n for n in q["queue"] if n["delivered"]]
        pending = [n for n in q["queue"] if not n["delivered"]]
        # Keep all pending + most recent delivered
        excess = len(q["queue"]) - MAX_QUEUE_SIZE
        q["queue"] = pending + delivered[max(excess, 0):]

    _save_queue(q)
    logger.info("Enqueued notification %s type=%s plan=%s", note_id, ntype, plan_name)
    return note_id


def fetch_all() -> list[dict]:
    """Return all undelivered notifications (oldest first)."""
    q = _load_queue()
    return [n for n in q["queue"] if not n["delivered"]]


def mark_delivered(ids: list[str]) -> int:
    """Mark notifications as delivered. Returns count of marked items."""
    if not ids:
        return 0
    q = _load_queue()
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    ids_set = set(ids)
    for note in q["queue"]:
        if note["id"] in ids_set and not note["delivered"]:
            note["delivered"] = True
            note["delivered_at"] = now
            count += 1
    _save_queue(q)
    return count


def get_pending_text() -> str:
    """Return pending notifications as human-readable text (for CLI output).

    Returns empty string if no pending notifications.
    """
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
    q = _load_queue()
    before = len(q["queue"])
    q["queue"] = [n for n in q["queue"] if not n["delivered"]]
    _save_queue(q)
    return before - len(q["queue"])
