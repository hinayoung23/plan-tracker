"""JSON file storage for plan-tracker data.

All data lives under ~/mcp-servers/plan-tracker/data/.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INDEX_FILE = DATA_DIR / "plan-index.json"


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_index() -> dict:
    """Load plan index, return empty dict if not found."""
    _ensure_data_dir()
    if not INDEX_FILE.exists():
        return {"plans": []}
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_index(index: dict) -> None:
    """Save plan index."""
    _ensure_data_dir()
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def plan_path(plan_name: str) -> Path:
    return DATA_DIR / f"{plan_name}.json"


def load_plan(plan_name: str) -> dict | None:
    """Load a single plan, return None if not found."""
    _ensure_data_dir()
    path = plan_path(plan_name)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_plan(plan_name: str, plan: dict) -> None:
    """Save a plan to disk."""
    _ensure_data_dir()
    plan["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(plan_path(plan_name), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)


def delete_plan_file(plan_name: str) -> bool:
    """Delete a plan file, return True if deleted."""
    path = plan_path(plan_name)
    if path.exists():
        path.unlink()
        return True
    return False


def update_index_entry(plan_name: str, plan: dict) -> None:
    """Update or append an entry in the plan index."""
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
    index = load_index()
    index["plans"] = [p for p in index["plans"] if p["name"] != plan_name]
    save_index(index)


def _compute_plan_status(plan: dict) -> str:
    """Compute plan-level status based on progress and dates."""
    milestones = plan.get("milestones", [])
    if not milestones:
        return "paused"
    completed = sum(1 for m in milestones if m["status"] == "completed")
    if completed == len(milestones):
        return "completed"

    target = plan.get("target_end_date", "")
    blocked = any(m["status"] == "blocked" for m in milestones)
    in_progress = any(m["status"] == "in_progress" for m in milestones)
    past_due = any(
        m["status"] in ("in_progress", "pending")
        and m.get("target_date", "")
        and m["target_date"] < datetime.now().strftime("%Y-%m-%d")
        for m in milestones
    )

    if not in_progress and not completed:
        return "paused"
    if past_due or blocked:
        return "behind"
    if completed >= len(milestones):
        return "completed"
    return "on_track"
