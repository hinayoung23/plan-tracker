"""Basic smoke tests for plan-tracker.

Run: python -m pytest test_smoke.py -v
Or:   python test_smoke.py
"""

import json
import os
import sys
import tempfile
import threading
from pathlib import Path


def _setup_test_env():
    """Create isolated temp directory and point plan-tracker at it."""
    tmpdir = tempfile.mkdtemp(prefix="plan-tracker-test-")
    os.environ["PLAN_TRACKER_DATA_DIR"] = tmpdir
    # Force re-import with new DATA_DIR
    import plan_tracker.storage as storage
    storage.DATA_DIR = Path(tmpdir)
    storage.INDEX_FILE = storage.DATA_DIR / "plan-index.json"
    import plan_tracker.notification_queue as nq
    nq.QUEUE_FILE = storage.DATA_DIR / "notification_queue.json"
    import plan_tracker.daily_tracker as dt
    dt.STATE_FILE = storage.DATA_DIR / "daily_state.json"
    import plan_tracker.reminder as rem
    rem.STATE_FILE = storage.DATA_DIR / ".reminder_state.json"
    rem.RESCHEDULE_MARKER = storage.DATA_DIR / ".reschedule_needed"
    return tmpdir


def test_create_and_get_plan():
    tmpdir = _setup_test_env()
    from plan_tracker.plan_manager import create_plan, get_plan, list_plans, delete_plan

    plan = create_plan("test-plan", "Test", "Goal here", "2026-12-31")
    assert plan["name"] == "test-plan"
    assert plan["title"] == "Test"

    got = get_plan("test-plan")
    assert got is not None
    assert got["name"] == "test-plan"
    assert got["reminders"]["email"]["api_secret"] == "***"  # sanitized

    plans = list_plans()
    assert any(p["name"] == "test-plan" for p in plans)

    delete_plan("test-plan")
    assert get_plan("test-plan") is None


def test_path_traversal_blocked():
    _setup_test_env()
    from plan_tracker.storage import validate_plan_name

    for bad in ("../openclaw", "..%2fetc", "has space", "UPPER", "", "a" * 65):
        try:
            validate_plan_name(bad)
            assert False, f"Should have rejected: {bad}"
        except ValueError:
            pass


def test_sanitize_plan():
    _setup_test_env()
    from plan_tracker.storage import sanitize_plan

    plan = {"reminders": {"email": {"api_secret": "sk-secret-123", "api_key_id": "key1"}}}
    s = sanitize_plan(plan)
    assert s["reminders"]["email"]["api_secret"] == "***"
    assert s["reminders"]["email"]["api_key_id"] == "key1"


def test_create_and_checkin():
    tmpdir = _setup_test_env()
    from plan_tracker.plan_manager import create_plan, delete_plan
    from plan_tracker.milestone_manager import add_milestone, add_checkin, get_current_milestone

    create_plan("test-ci", "CI Test", "Test checkin", "2026-12-31",
                milestones=[{"title": "M1", "target_date": "2026-08-01", "effort_hours_estimate": 10}])

    ms = get_current_milestone("test-ci")
    assert ms is not None
    assert ms["title"] == "M1"

    result = add_checkin("test-ci", ms["id"], progress_pct=50, hours_spent=2, morale="good")
    assert result["completion_pct"] == 50
    assert result["status"] == "in_progress"

    result = add_checkin("test-ci", ms["id"], progress_pct=100, hours_spent=3)
    assert result["status"] == "completed"

    delete_plan("test-ci")


def test_concurrent_milestone_adds():
    """Verify concurrent milestone additions don't lose data."""
    tmpdir = _setup_test_env()
    from plan_tracker.plan_manager import create_plan, delete_plan
    from plan_tracker.milestone_manager import add_milestone

    create_plan("test-race", "Race Test", "Test", "2026-12-31")

    errors = []
    def do_add(i):
        try:
            add_milestone("test-race", {
                "title": f"MS-{i}", "target_date": "2026-08-01",
                "effort_hours_estimate": i + 1,
            })
        except Exception as e:
            errors.append((i, str(e)))

    threads = [threading.Thread(target=do_add, args=(i,)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    from plan_tracker.storage import load_plan
    plan = load_plan("test-race")
    count = len(plan.get("milestones", []))
    expected = 30
    assert count == expected, f"Expected {expected} milestones, got {count}. Last error: {errors[-1] if errors else 'none'}"

    # Verify no duplicate IDs
    ids = [m["id"] for m in plan["milestones"]]
    assert len(ids) == len(set(ids)), f"Duplicate IDs found: {ids}"

    delete_plan("test-race")


def test_notification_queue_locking():
    """Verify concurrent queue writes don't lose data."""
    tmpdir = _setup_test_env()
    from plan_tracker.notification_queue import enqueue, fetch_all, mark_delivered

    ids = []
    errors = []
    def do_enqueue(i):
        try:
            nid = enqueue("test", "info", f"msg {i}")
            ids.append(nid)
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=do_enqueue, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    pending = fetch_all()
    assert len(pending) == 40, f"Expected 40, got {len(pending)}. Errors: {errors}"

    # Clean up
    mark_delivered([n["id"] for n in pending])


if __name__ == "__main__":
    print("Running smoke tests...")
    test_create_and_get_plan()
    print("  ✓ test_create_and_get_plan")
    test_path_traversal_blocked()
    print("  ✓ test_path_traversal_blocked")
    test_sanitize_plan()
    print("  ✓ test_sanitize_plan")
    test_create_and_checkin()
    print("  ✓ test_create_and_checkin")
    test_concurrent_milestone_adds()
    print("  ✓ test_concurrent_milestone_adds")
    test_notification_queue_locking()
    print("  ✓ test_notification_queue_locking")
    print("ALL TESTS PASSED")
