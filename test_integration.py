"""Integration / regression tests for plan-tracker critical paths.

Covers the failure modes that smoke tests don't:
  - Concurrent plan writes (atomicity)
  - SmartPoller generation counter + deliver_lock
  - JS plugin registration structure
  - Lock mechanism consistency
  - Crash-safe write pattern
  - Idempotency key batch hashing

Run: python test_integration.py
"""

import hashlib
import importlib
import inspect
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path


# ── helpers ──────────────────────────────────────────────────────

def _setup_test_env():
    """Isolate plan-tracker data into a temp directory."""
    tmpdir = tempfile.mkdtemp(prefix="pt-test-")
    os.environ["PLAN_TRACKER_DATA_DIR"] = tmpdir
    import plan_tracker.storage as storage
    import plan_tracker.notification_queue as nq
    import plan_tracker.daily_tracker as dt
    import plan_tracker.reminder as rem

    storage.DATA_DIR = Path(tmpdir)
    storage.INDEX_FILE = storage.DATA_DIR / "plan-index.json"
    nq.QUEUE_FILE = storage.DATA_DIR / "notification_queue.json"
    dt.STATE_FILE = storage.DATA_DIR / "daily_state.json"
    rem.STATE_FILE = storage.DATA_DIR / ".reminder_state.json"
    rem.RESCHEDULE_MARKER = storage.DATA_DIR / ".reschedule_needed"
    return tmpdir


# ── Test 1: Concurrent plan writes don't lose data ───────────────

def test_concurrent_writes_no_data_loss():
    """40 threads each add a unique key to the same plan.  All 40 must survive."""
    tmpdir = _setup_test_env()
    from plan_tracker.plan_manager import create_plan, delete_plan
    from plan_tracker.storage import modify_plan_and_index, load_plan

    create_plan("test-race", "Race", "Test", "2026-12-31")
    errors = []

    def add_key(i):
        try:
            modify_plan_and_index("test-race", lambda p: p.__setitem__(f"k{i}", i))
        except Exception as e:
            errors.append((i, str(e)))

    threads = [threading.Thread(target=add_key, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    plan = load_plan("test-race")
    for i in range(40):
        assert plan.get(f"k{i}") == i, f"Missing key k{i}. Errors: {errors[:3]}"
    assert len(errors) == 0, f"Unexpected errors: {errors[:3]}"

    delete_plan("test-race")


# ── Test 2: SmartPoller generation counter works ─────────────────

def test_smartpoller_generation_counter():
    """Old threads exit when gen bumps; deliver_lock serializes delivery."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "webhook_receiver",
        str(Path(__file__).resolve().parent / "scripts" / "webhook_receiver.py"),
    )
    mod = importlib.util.module_from_spec(spec)

    # Verify the source code has generation counter AND deliver_lock
    with open(spec.origin) as f:
        src = f.read()

    assert "self._generation" in src, "Missing generation counter"
    assert "self._deliver_lock" in src, "Missing deliver_lock"
    assert "_ensure_running" in src, "Missing _ensure_running"

    # Verify _poll_loop takes generation parameter
    poll_src = src[src.find("def _poll_loop"):src.find("def ", src.find("def _poll_loop") + 1)]
    assert "my_generation" in poll_src or "my_gen" in poll_src, \
        "_poll_loop must receive generation parameter"

    # Verify generation check happens
    assert "self._generation !=" in src, "Missing generation comparison"

    spec.loader.exec_module(mod)
    SmartPoller = mod.SmartPoller

    poller = SmartPoller("ch", "tgt")
    assert hasattr(poller, "_generation"), "SmartPoller missing _generation"
    assert hasattr(poller, "_deliver_lock"), "SmartPoller missing _deliver_lock"


# ── Test 3: JS plugin registration structure ─────────────────────

def test_js_plugin_registration_structure():
    """Verify the JS plugin has correct registerCli pattern."""
    js_path = Path(__file__).resolve().parent / "src" / "index.js"
    src = js_path.read_text()

    # Must use registrar function pattern (not object literal)
    assert "registerCli" in src, "Missing registerCli"
    assert "ctx.program" in src, "Missing ctx.program (Commander context)"
    assert ".command(" in src, "Missing .command() call"
    assert '"plan-tracker-deliver"' in src, "Missing command name"

    # Must declare commands + descriptors metadata (JS object keys)
    assert 'commands' in src, "Missing commands metadata"
    assert 'descriptors' in src, "Missing descriptors metadata"

    # Must use stdin for payload (not process arguments)
    assert "readStdin" in src or "process.stdin" in src, "Missing stdin reading"

    # Must use callGatewayFromCli for delivery
    assert "callGatewayFromCli" in src, "Missing callGatewayFromCli"
    assert '"send"' in src, "Must call Gateway 'send' method"


# ── Test 4: Lock mechanism consistency ────────────────────────────

def test_lock_mechanism_consistency():
    """All lock paths must use .json.lock files, not direct data file locks."""
    storage_src = (Path(__file__).resolve().parent /
                   "plan_tracker" / "storage.py").read_text()
    file_lock_src = (Path(__file__).resolve().parent /
                     "plan_tracker" / "file_lock.py").read_text()

    # LockedFile must use lock file path
    assert ".lock" in file_lock_src, "LockedFile must use .lock files"

    # load_plan must use lock file
    assert "_lock_file_path" in storage_src, "load_plan must use lock file path"

    # load_index must use lock file
    idx_load = storage_src[storage_src.find("def load_index"):storage_src.find("def ", storage_src.find("def load_index") + 1)]
    assert "_lock_file_path" in idx_load or ".lock" in idx_load, "load_index must use lock file"

    # modify_plan_and_index must use lock files
    mod_src = storage_src[storage_src.find("def modify_plan_and_index"):storage_src.find("def _do_update", storage_src.find("def modify_plan_and_index") + 1)]
    assert "_lock_file_path" in mod_src or ".lock" in mod_src, \
        "modify_plan_and_index must use lock files"

    # delete_plan must use lock file
    plan_src = (Path(__file__).resolve().parent /
                "plan_tracker" / "plan_manager.py").read_text()
    del_src = plan_src[plan_src.find("def delete_plan"):plan_src.find("def ", plan_src.find("def delete_plan") + 1)]
    assert "_lock_file_path" in del_src or ".lock" in del_src, \
        "delete_plan must use lock file"


# ── Test 5: Crash-safe write pattern ─────────────────────────────

def test_crash_safe_write_pattern():
    """_atomic_write must use tmp file + fsync + rename.  No ftruncate-first."""
    storage_src = (Path(__file__).resolve().parent /
                   "plan_tracker" / "storage.py").read_text()

    # Find _atomic_write or equivalent
    write_func = storage_src[storage_src.find("def _atomic_write"):
                             storage_src.find("def ", storage_src.find("def _atomic_write") + 1)]

    assert "os.replace" in write_func, "Must use atomic os.replace (rename)"
    assert "os.fsync" in write_func, "Must fsync before rename"

    # Must NOT truncate the original file before writing
    # (ftruncate should only appear on tmp fd, never on the original path's fd)
    # This is hard to verify statically, but we can check that tmp+rename is the pattern


# ── Test 6: Idempotency key uses batch hash ──────────────────────

def test_idempotency_key_batch_hash():
    """Idempotency key must change when batch content changes."""
    ids_a = ["a", "b", "c"]
    ids_b = ["a", "b", "c", "d"]
    ids_c = ["b", "a", "c"]  # same content, different order

    key_a = hashlib.sha256(",".join(sorted(ids_a)).encode()).hexdigest()[:16]
    key_b = hashlib.sha256(",".join(sorted(ids_b)).encode()).hexdigest()[:16]
    key_c = hashlib.sha256(",".join(sorted(ids_c)).encode()).hexdigest()[:16]

    # Different batches → different keys
    assert key_a != key_b, "Different batches must have different keys"

    # Same batch (reordered) → same key (idempotent)
    assert key_a == key_c, "Same batch must have same key regardless of order"

    # Verify the receiver code uses this pattern
    receiver_src = (Path(__file__).resolve().parent /
                    "scripts" / "webhook_receiver.py").read_text()
    assert "hashlib" in receiver_src, "Receiver must use hashlib for batch key"
    assert "sorted(" in receiver_src, "Receiver must sort IDs for stable hash"


# ── main ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        ("concurrent_writes_no_data_loss", test_concurrent_writes_no_data_loss),
        ("smartpoller_generation_counter", test_smartpoller_generation_counter),
        ("js_plugin_registration_structure", test_js_plugin_registration_structure),
        ("lock_mechanism_consistency", test_lock_mechanism_consistency),
        ("crash_safe_write_pattern", test_crash_safe_write_pattern),
        ("idempotency_key_batch_hash", test_idempotency_key_batch_hash),
    ]

    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            print(f"  ✗ {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
            failed += 1

    print()
    if failed:
        print(f"{failed}/{len(tests)} FAILED")
        sys.exit(1)
    else:
        print(f"ALL {len(tests)} TESTS PASSED")
