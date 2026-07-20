"""JSON file storage for plan-tracker data.

All data lives under ~/mcp-servers/plan-tracker/data/ by default.
"""

import fcntl
import hashlib
import json
import logging
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
    """Return a deep copy of *plan* with sensitive fields masked.

    Must be called on every plan dict before it is returned to the AI
    context (plan_get, plan_list, plan_update, reminder_configure, etc.).
    """
    if not plan:
        return plan

    def _sanitize(value):
        if isinstance(value, dict):
            return {
                key: "***" if key in _SENSITIVE_KEYS else _sanitize(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [_sanitize(item) for item in value]
        return value

    return _sanitize(plan)


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
logger = logging.getLogger("plan_tracker.storage")


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)


# Establish the privacy boundary once for every CLI/MCP process.  Individual
# writers still call this helper in case the directory is removed at runtime.
_ensure_data_dir()


def load_index() -> dict:
    """Return an index rebuilt from authoritative plan files.

    ``plan-index.json`` is a derived cache, never a second source of truth.
    Rebuilding under the index lock makes a crash between the plan write and
    cache refresh invisible to callers while retaining the fast on-disk cache
    for diagnostics and older clients.
    """
    _ensure_data_dir()
    lock_path = _lock_file_path(INDEX_FILE)
    try:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return _rebuild_index()
    try:
        # Fast path: validate the cache using file metadata only.  This avoids
        # reparsing every plan on the frequent reminder-engine read path.
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        try:
            cached = json.loads(INDEX_FILE.read_bytes())
        except (json.JSONDecodeError, FileNotFoundError, ValueError):
            cached = None
        if (
            isinstance(cached, dict)
            and cached.get("_source_signature") == _plan_source_signature()
            and isinstance(cached.get("plans"), list)
        ):
            return cached

        # Upgrade to exclusive lock and recheck in case another process
        # refreshed the cache while we were switching lock modes.
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            cached = json.loads(INDEX_FILE.read_bytes())
        except (json.JSONDecodeError, FileNotFoundError, ValueError):
            cached = None
        signature = _plan_source_signature()
        if (
            isinstance(cached, dict)
            and cached.get("_source_signature") == signature
            and isinstance(cached.get("plans"), list)
        ):
            return cached

        rebuilt = _rebuild_index()
        if cached != rebuilt:
            try:
                _atomic_write(INDEX_FILE, rebuilt)
            except OSError:
                logger.warning("Failed to refresh derived plan index cache", exc_info=True)
        return rebuilt
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


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
    # Use lock file for consistent locking (avoids inode-staleness with
    # _atomic_write's rename in modify_plan_and_index).
    lock_path = _lock_file_path(path)
    try:
        lock_fd = os.open(lock_path, os.O_RDONLY | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        try:
            return json.loads(path.read_bytes())
        except (json.JSONDecodeError, FileNotFoundError):
            return None
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


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
    """Atomically update a plan and refresh its derived index cache.

    The plan file is the sole source of truth.  Once its atomic rename
    succeeds the operation is committed; a failed index-cache write is
    recoverable because :func:`load_index` always rebuilds from plan files.
    """
    validate_plan_name(plan_name)
    _ensure_data_dir()
    plan_path = DATA_DIR / f"{plan_name}.json"

    # Acquire lock via a separate lock file.  Using the data file
    # directly for flock breaks with atomic rename (rename changes
    # the inode, so other threads' open fds point to stale data).
    plan_lock = _lock_file_path(plan_path)
    plan_lock.parent.mkdir(parents=True, exist_ok=True)
    plan_lock_fd = os.open(plan_lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(plan_lock_fd, fcntl.LOCK_EX)

        # Read plan (fresh open each time to get latest inode)
        try:
            plan_data = plan_path.read_bytes()
            plan = json.loads(plan_data) if plan_data else {}
        except FileNotFoundError:
            if create:
                plan = {}
            else:
                raise ValueError(f"Plan '{plan_name}' not found")
        except (json.JSONDecodeError, ValueError):
            if create:
                plan = {}
            else:
                raise ValueError(f"Plan '{plan_name}' data is corrupted")

        plan["updated_at"] = datetime.now(timezone.utc).isoformat()
        modifier_fn(plan)

        # Serialize the authoritative plan commit with cache readers/writers.
        idx_lock = _lock_file_path(INDEX_FILE)
        idx_lock_fd = os.open(idx_lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(idx_lock_fd, fcntl.LOCK_EX)
            # Commit the authoritative body first.
            _atomic_write(plan_path, plan)
            # Refresh the derived cache best-effort.  Cache failure must not
            # turn a successfully committed body update into a false failure.
            try:
                _atomic_write(INDEX_FILE, _rebuild_index())
            except OSError:
                logger.warning(
                    "Plan committed but derived index cache refresh failed for %s",
                    plan_name,
                    exc_info=True,
                )
        finally:
            fcntl.flock(idx_lock_fd, fcntl.LOCK_UN)
            os.close(idx_lock_fd)
        result = dict(plan)
    finally:
        fcntl.flock(plan_lock_fd, fcntl.LOCK_UN)
        os.close(plan_lock_fd)

    return result


def _atomic_write(path: Path, data: dict) -> None:
    """Crash-safe atomic write: tmp file → fsync → rename.

    The caller must hold the lock file for *path* to prevent
    concurrent writers from reading a stale inode after rename.
    """
    tmp_path = path.with_suffix(f".tmp-{os.getpid()}")
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        tmp_fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            written = 0
            while written < len(payload):
                n = os.write(tmp_fd, payload[written:])
                if n <= 0:
                    raise OSError("write returned 0")
                written += n
            os.fsync(tmp_fd)
        finally:
            os.close(tmp_fd)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _lock_file_path(data_path: Path) -> Path:
    """Return the lock-file path for a data file."""
    return data_path.with_suffix(data_path.suffix + ".lock")


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
    """Rebuild the plan index from plan files on disk.

    Only includes JSON files that look like plan documents
    (have 'name' and 'milestones' fields matching the filename).
    System files (daily_state, notification_queue, etc.) are excluded.
    """
    result: dict = {"plans": []}
    try:
        for path in sorted(DATA_DIR.glob("*.json")):
            if path.name in _SYSTEM_FILES:
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    plan = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            # Validate: must be a dict with matching name field
            if not isinstance(plan, dict):
                continue
            name = plan.get("name", "")
            if not name or not isinstance(name, str):
                continue
            # Filename must match plan name
            if path.stem != name:
                continue
            milestones = plan.get("milestones", [])
            if not isinstance(milestones, list):
                continue
            completed = sum(1 for m in milestones if isinstance(m, dict) and m.get("status") == "completed")
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
    except OSError:
        pass
    result["_source_signature"] = _plan_source_signature()
    return result


_SYSTEM_FILES = {
    "plan-index.json", "notification_queue.json", "daily_state.json",
    ".reminder_state.json", "webhook_delivery.json",
}


def _plan_source_signature() -> str:
    """Cheap fingerprint of authoritative plan files for cache validation."""
    digest = hashlib.sha256()
    try:
        for path in sorted(DATA_DIR.glob("*.json")):
            if path.name in _SYSTEM_FILES:
                continue
            try:
                stat_result = path.stat()
            except OSError:
                continue
            digest.update(path.name.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            digest.update(str(stat_result.st_size).encode())
            digest.update(b":")
            digest.update(str(stat_result.st_mtime_ns).encode())
            digest.update(b"\0")
    except OSError:
        pass
    return digest.hexdigest()


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
