"""Thread-safe + process-safe file locking for plan-tracker data files.

Uses separate lock files (.json.lock) to avoid inode-staleness when
concurrent writers use atomic tmp+rename.  The data file is always
read fresh after acquiring the lock and written via tmp+fsync+rename.
"""

import fcntl
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _get_thread_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _locks_lock:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


@contextmanager
def LockedFile(path: Path, default: dict | list | None = None):
    """Exclusive read-modify-write context manager for a JSON file.

    Uses a separate .lock file for fcntl flock to avoid inode-staleness
    when writers use atomic tmp+rename.
    """
    if default is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    thread_lock = _get_thread_lock(lock_path)

    with thread_lock:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)

            # Read fresh data after acquiring lock
            try:
                data = json.loads(path.read_bytes()) if path.exists() else default
            except (json.JSONDecodeError, ValueError, FileNotFoundError):
                data = default

            if data is None:
                raise FileNotFoundError(f"File not found: {path}")

            yield data

            # Write via atomic tmp+rename (clean up tmp on failure)
            tmp_path = path.with_suffix(f".tmp-{os.getpid()}")
            payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            try:
                tmp_fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def safe_write_json(path: Path, data: dict | list) -> None:
    """Atomic write with proper permissions (0600 from creation)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{os.getpid()}-{id(path)}")
    # Create with 0600 directly — never world-readable even momentarily
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        written = 0
        while written < len(payload):
            n = os.write(fd, payload[written:])
            if n <= 0:
                raise OSError("write returned 0")
            written += n
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.close(fd)
    os.replace(tmp, path)
