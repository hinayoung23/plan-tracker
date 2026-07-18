"""Thread-safe + process-safe file locking for plan-tracker data files.

Provides ``LockedFile``, a context manager that combines an in-process
``threading.Lock`` with a cross-process ``fcntl.flock``, so that
concurrent writers never see partial or corrupted data.
"""

import fcntl
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

# ── per-file in-process locks ────────────────────────────────────
_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _get_thread_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _locks_lock:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


# ── public API ────────────────────────────────────────────────────

@contextmanager
def LockedFile(path: Path, default: dict | list | None = None):
    """Exclusive read-modify-write context manager for a JSON file.

    Combines an in-process ``threading.Lock`` with an inter-process
    ``fcntl.flock(LOCK_EX)``, so both threads AND subprocesses see
    a consistent view.

    Usage::

        with LockedFile(QUEUE_FILE, default={"queue": []}) as q:
            q["queue"].append(item)
            # … more modifications …
        # file is atomically written on exit
    """
    if default is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _get_thread_lock(path)

    with thread_lock:
        try:
            f = open(path, "r+")
        except FileNotFoundError:
            if default is None:
                raise FileNotFoundError(f"File not found: {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            f = open(path, "w+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                data = json.load(f)
            except (json.JSONDecodeError, ValueError):
                data = default
            yield data
            f.seek(0)
            f.truncate()
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
            # Set restrictive permissions (owner-only read/write)
            os.fchmod(f.fileno(), 0o600)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()


def safe_write_json(path: Path, data: dict | list) -> None:
    """Atomic write of *data* to *path* with proper permissions.

    Writes to a temp file, fsyncs, renames, and chmods to 0600 so
    that sensitive data (API keys, etc.) is never world-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    tmp.replace(path)
