"""JSON file storage for plan-tracker data.

All data lives under ~/mcp-servers/plan-tracker/data/ by default.
"""

import fcntl
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from plan_tracker.file_lock import LockedFile, safe_write_json

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
    if "api_secret" in email:
        email = dict(email, api_secret="***")
        plan.setdefault("reminders", {})["email"] = email
    return plan


def _resolve_data_dir() -> Path:
    """Resolve the data directory across install methods.

    1. PLAN_TRACKER_DATA_DIR env var (explicit override — set this in
       your MCP server config for consistent cross-process paths).
    2. ``<project-root>/data/`` (source / editable install, must
       contain .gitkeep or plan-index.json).
    3. ``~/mcp-servers/plan-tracker/data/`` (wheel / site-packages).
    """
    env_dir = os.environ.get("PLAN_TRACKER_DATA_DIR")
    if env_dir:
        return Path(env_dir)

    computed = Path(__file__).resolve().parent.parent / "data"
    if computed.is_dir() and (
        (computed / ".gitkeep").exists() or (computed / "plan-index.json").exists()
    ):
        return computed

    # Wheel/site-packages install — use well-known absolute path.
    # This path is shared by daemon, MCP, CLI, and webhook_receiver.
    return Path.home() / "mcp-servers" / "plan-tracker" / "data"


DATA_DIR = _resolve_data_dir()
INDEX_FILE = DATA_DIR / "plan-index.json"


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_index() -> dict:
    """Load plan index with shared read lock."""
    _ensure_data_dir()
    if not INDEX_FILE.exists():
        return {"plans": []}
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            return json.load(f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def save_index(index: dict) -> None:
    """Save plan index with exclusive file lock."""
    safe_write_json(INDEX_FILE, index)


def plan_path(plan_name: str) -> Path:
    validate_plan_name(plan_name)
    return DATA_DIR / f"{plan_name}.json"


def load_plan(plan_name: str) -> dict | None:
    """Load a single plan with shared read lock, return None if not found."""
    validate_plan_name(plan_name)
    _ensure_data_dir()
    path = DATA_DIR / f"{plan_name}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            return json.load(f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def save_plan(plan_name: str, plan: dict) -> None:
    """Save a plan to disk atomically."""
    validate_plan_name(plan_name)
    _ensure_data_dir()
    plan["updated_at"] = datetime.now(timezone.utc).isoformat()
    copy = dict(plan)
    _do_write_plan(plan_name, copy)


def _do_write_plan(plan_name: str, plan: dict) -> None:
    """Write plan dict to disk under exclusive lock."""
    path = DATA_DIR / f"{plan_name}.json"
    with LockedFile(path, default={}) as current:
        current.clear()
        current.update(plan)


def modify_plan(plan_name: str, modifier_fn) -> dict | None:
    """Atomically read-modify-write a plan under exclusive lock.

    *modifier_fn* receives the plan dict and modifies it IN-PLACE.
    It must NOT return the dict (return value is ignored).
    The plan is saved automatically after the function returns.
    Returns the final plan dict.

    Note: callers should use *modify_plan_and_index* instead to
    ensure the plan index stays in sync with the plan body.
    """
    validate_plan_name(plan_name)
    _ensure_data_dir()
    path = DATA_DIR / f"{plan_name}.json"
    try:
        with LockedFile(path, default=None) as current:
            if current is None:
                raise ValueError(f"Plan '{plan_name}' not found")
            current["updated_at"] = datetime.now(timezone.utc).isoformat()
            modifier_fn(current)
            return dict(current)
    except FileNotFoundError:
        raise ValueError(f"Plan '{plan_name}' not found")


def modify_plan_and_index(plan_name: str, modifier_fn,
                         create: bool = False) -> dict:
    """Atomic plan + index update with BOTH locks held simultaneously.

    Uses raw fcntl to hold the plan file lock AND index file lock at
    the same time, so no concurrent writer can observe body-index
    disagreement.  Plan writes first, then index.
    """
    validate_plan_name(plan_name)
    _ensure_data_dir()
    plan_path = DATA_DIR / f"{plan_name}.json"

    # Open plan file with dual fcntl locks (plan + index).
    try:
        plan_fd = os.open(plan_path, os.O_RDWR)
    except FileNotFoundError:
        if create:
            plan_fd = os.open(plan_path, os.O_RDWR | os.O_CREAT, 0o600)
        else:
            raise ValueError(f"Plan '{plan_name}' not found")
    os.fchmod(plan_fd, 0o600)
    try:
        fcntl.flock(plan_fd, fcntl.LOCK_EX)
        plan_data = _read_full(plan_fd)
        try:
            plan = json.loads(plan_data) if plan_data else {}
        except (json.JSONDecodeError, ValueError):
            if create:
                plan = {}
            else:
                raise ValueError(f"Plan '{plan_name}' data is corrupted")

        plan["updated_at"] = datetime.now(timezone.utc).isoformat()
        modifier_fn(plan)

        # Open + lock index (both plan and index locks held)
        index_path = INDEX_FILE
        try:
            index_fd = os.open(index_path, os.O_RDWR)
        except FileNotFoundError:
            index_fd = os.open(index_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(index_fd, 0o600)
        try:
            fcntl.flock(index_fd, fcntl.LOCK_EX)
            idx_data = _read_full(index_fd)
            try:
                index = json.loads(idx_data) if idx_data else {"plans": []}
            except (json.JSONDecodeError, ValueError):
                # Attempt to rebuild from plan files
                index = _rebuild_index()
            _do_update_index_entry(plan_name, plan, index)

            # Write PLAN first (if this fails, index is unchanged)
            _write_and_fsync(plan_fd, plan)
            # Write INDEX second
            _write_and_fsync(index_fd, index)
        finally:
            fcntl.flock(index_fd, fcntl.LOCK_UN)
            os.close(index_fd)
        result = dict(plan)
    finally:
        fcntl.flock(plan_fd, fcntl.LOCK_UN)
        os.close(plan_fd)

    return result


def _write_and_fsync(fd: int, data: dict) -> None:
    """Write *data* to *fd* crash-safely.

    Writes BEFORE truncating — if the write or fsync fails, the file
    still contains the old valid data.  Loops on os.write to handle
    partial writes.
    """
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    written = 0
    while written < len(payload):
        n = os.write(fd, payload[written:])
        if n < 0:
            raise OSError("write failed")
        written += n
    os.fsync(fd)
    # Only truncate after confirmed write+fsync
    os.ftruncate(fd, len(payload))


def _read_full(fd: int, max_bytes: int = 100_000_000) -> bytes:
    """Read *fd* from position 0 until EOF or *max_bytes*."""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    total = 0
    while total < max_bytes:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _rebuild_index() -> dict:
    """Rebuild the plan index from plan files on disk."""
    result: dict = {"plans": []}
    try:
        for path in sorted(DATA_DIR.glob("*.json")):
            if path.name == "plan-index.json":
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    plan = json.load(f)
                name = plan.get("name", path.stem)
                if not name:
                    continue
                milestones = plan.get("milestones", [])
                completed = sum(1 for m in milestones if m["status"] == "completed")
                total = len(milestones)
                result["plans"].append({
                    "name": name,
                    "title": plan.get("title", name),
                    "category": plan.get("category", "custom"),
                    "target_end_date": plan.get("target_end_date", ""),
                    "total_milestones": total,
                    "completed_milestones": completed,
                    "overall_progress_pct": round(completed / total * 100) if total else 0,
                    "status": _compute_plan_status(plan),
                    "updated_at": plan.get("updated_at", ""),
                })
            except (json.JSONDecodeError, OSError, KeyError):
                continue
    except OSError:
        pass
    return result


def _do_update_index_entry(plan_name: str, plan: dict, index: dict) -> None:
    """Update index entry in-place (index lock held by caller)."""
    milestones = plan.get("milestones", [])
    completed = sum(1 for m in milestones if m["status"] == "completed")
    total = len(milestones)
    overall = round(completed / total * 100) if total else 0
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
            return
    index["plans"].append(entry)


def delete_plan_file(plan_name: str) -> bool:
    """Delete a plan file, return True if deleted."""
    validate_plan_name(plan_name)
    path = DATA_DIR / f"{plan_name}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def update_index_entry(plan_name: str, plan: dict) -> None:
    """Update or append an entry in the plan index (atomic)."""
    validate_plan_name(plan_name)
    milestones = plan.get("milestones", [])
    completed = sum(1 for m in milestones if m["status"] == "completed")
    total = len(milestones)
    overall = round(completed / total * 100) if total else 0
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

    with LockedFile(INDEX_FILE, default={"plans": []}) as index:
        for i, p in enumerate(index["plans"]):
            if p["name"] == plan_name:
                index["plans"][i] = entry
                break
        else:
            index["plans"].append(entry)


def remove_index_entry(plan_name: str) -> None:
    """Remove a plan from the index (atomic)."""
    validate_plan_name(plan_name)
    with LockedFile(INDEX_FILE, default={"plans": []}) as index:
        index["plans"] = [p for p in index["plans"] if p["name"] != plan_name]


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
