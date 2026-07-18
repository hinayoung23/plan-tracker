"""JSON file storage for plan-tracker data.

All data lives under ~/mcp-servers/plan-tracker/data/ by default.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# Only kebab-case alphanumeric names, 1-64 chars, no path separators
_VALID_PLAN_NAME = re.compile(r'^[a-z][a-z0-9]*(-[a-z0-9]+)*$')
_MAX_NAME_LEN = 64

# Sensitive keys that must never be returned to AI context
_SENSITIVE_KEYS = {"api_secret"}


def validate_plan_name(name: str) -> None:
    """Raise ValueError if *name* is not a safe plan identifier."""
    if not name or not isinstance(name, str):
        raise ValueError("Plan name must be a non-empty string")
    if len(name) > _MAX_NAME_LEN:
        raise ValueError(f"Plan name too long (max {_MAX_NAME_LEN})")
    if ".." in name or "/" in name or "\\" in name:
        raise ValueError("Plan name must not contain path separators")
    if not _VALID_PLAN_NAME.match(name):
        raise ValueError("Plan name must be kebab-case alphanumeric (e.g. my-plan)")


def sanitize_plan(plan: dict) -> dict:
    """Return a copy of *plan* with sensitive fields masked.

    Must be called on every plan dict before it is returned to the AI
    context (plan_get, plan_list, plan_update, reminder_configure, etc.).
    """
    if not plan:
        return plan
    email = plan.get("reminders", {}).get("email", {})
    if email.get("api_secret"):
        email = dict(email, api_secret="***")
        plan.setdefault("reminders", {})["email"] = email
    return plan


def _resolve_data_dir() -> Path:
    """Resolve the data directory across install methods.

    1. PLAN_TRACKER_DATA_DIR env var (explicit override)
    2. ``<project-root>/data/`` (source / editable install)
    3. ``~/mcp-servers/plan-tracker/data/`` (wheel / site-packages install)
    """
    env_dir = os.environ.get("PLAN_TRACKER_DATA_DIR")
    if env_dir:
        return Path(env_dir)

    computed = Path(__file__).resolve().parent.parent / "data"
    if computed.is_dir():
        return computed

    return Path.home() / "mcp-servers" / "plan-tracker" / "data"


DATA_DIR = _resolve_data_dir()
INDEX_FILE = DATA_DIR / "plan-index.json"


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _write_atomic(path: Path, data: dict) -> None:
    """Write *data* as JSON to *path* atomically (tmp + rename)."""
    _ensure_data_dir()
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_index() -> dict:
    """Load plan index, return empty dict if not found."""
    _ensure_data_dir()
    if not INDEX_FILE.exists():
        return {"plans": []}
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_index(index: dict) -> None:
    """Save plan index atomically."""
    _write_atomic(INDEX_FILE, index)


def plan_path(plan_name: str) -> Path:
    validate_plan_name(plan_name)
    return DATA_DIR / f"{plan_name}.json"


def load_plan(plan_name: str) -> dict | None:
    """Load a single plan, return None if not found."""
    validate_plan_name(plan_name)
    _ensure_data_dir()
    path = DATA_DIR / f"{plan_name}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_plan(plan_name: str, plan: dict) -> None:
    """Save a plan to disk atomically."""
    validate_plan_name(plan_name)
    _ensure_data_dir()
    plan["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_atomic(DATA_DIR / f"{plan_name}.json", plan)


def delete_plan_file(plan_name: str) -> bool:
    """Delete a plan file, return True if deleted."""
    validate_plan_name(plan_name)
    path = DATA_DIR / f"{plan_name}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def update_index_entry(plan_name: str, plan: dict) -> None:
    """Update or append an entry in the plan index."""
    validate_plan_name(plan_name)
    index = load_index()
    milestones = plan.get("milestones", [])
    completed = sum(1 for m in milestones if m["status"] == "completed")
    total = len(milestones) or 1
    overall = round(completed / total * 100)

    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "name": plan_name,
        "title": plan.get("title", plan_name),
        "category": plan.get("category", "custom"),
        "target_end_date": plan.get("target_end_date", ""),
        "total_milestones": total,
        "completed_milestones": completed,
        "overall_progress_pct": overall,
        "status": _compute_plan_status(plan),
        "updated_at": now,
    }

    for i, p in enumerate(index["plans"]):
        if p["name"] == plan_name:
            index["plans"][i] = entry
            break
    else:
        index["plans"].append(entry)

    save_index(index)


def remove_index_entry(plan_name: str) -> None:
    """Remove a plan from the index."""
    validate_plan_name(plan_name)
    index = load_index()
    index["plans"] = [p for p in index["plans"] if p["name"] != plan_name]
    save_index(index)


def _compute_plan_status(plan: dict) -> str:
    """Compute plan-level status based on progress and dates.

    Single source of truth — used by both the index and plan_analysis.
    """
    milestones = plan.get("milestones", [])
    if not milestones:
        return "paused"
    completed = sum(1 for m in milestones if m["status"] == "completed")
    if completed == len(milestones):
        return "completed"
    blocked = any(m["status"] == "blocked" for m in milestones)
    in_progress = any(m["status"] == "in_progress" for m in milestones)
    if blocked and not in_progress:
        return "paused"
    if blocked:
        return "behind"
    past_due = any(
        m["status"] in ("in_progress", "pending")
        and m.get("target_date", "")
        and m["target_date"] < datetime.now().strftime("%Y-%m-%d")
        for m in milestones
    )
    if not in_progress and completed < len(milestones):
        return "paused"
    if past_due:
        return "behind"
    return "on_track"
