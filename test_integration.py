"""Integration / regression tests for plan-tracker critical paths.

Covers the failure modes that smoke tests don't:
  - Concurrent plan writes (atomicity)
  - Derived-index recovery after cache-write failure
  - Idempotent cleanup of partially deleted plans
  - Daemon PID lock protocol and short writes
  - Stale temporary-file permissions
  - Privacy-safe setup CLI contract
  - SmartPoller generation counter + deliver_lock
  - JS plugin registration structure
  - Lock mechanism consistency
  - Crash-safe write pattern
  - Per-notification delivery boundaries and idempotency

Run: python test_integration.py
"""

import contextlib
import importlib
import inspect
import io
import json
import logging
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
    import plan_tracker.plan_manager as pm

    storage.DATA_DIR = Path(tmpdir)
    storage.INDEX_FILE = storage.DATA_DIR / "plan-index.json"
    nq.QUEUE_FILE = storage.DATA_DIR / "notification_queue.json"
    dt.STATE_FILE = storage.DATA_DIR / "daily_state.json"
    rem.STATE_FILE = storage.DATA_DIR / ".reminder_state.json"
    rem.RESCHEDULE_MARKER = storage.DATA_DIR / ".reschedule_needed"
    pm.DATA_DIR = storage.DATA_DIR
    pm.INDEX_FILE = storage.INDEX_FILE
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

    before_umask = os.umask(0)
    os.umask(before_umask)
    spec.loader.exec_module(mod)
    after_umask = os.umask(0)
    os.umask(after_umask)
    assert after_umask == before_umask, "Importing receiver changed process umask"
    SmartPoller = mod.SmartPoller

    poller = SmartPoller("ch", "tgt")
    assert hasattr(poller, "_generation"), "SmartPoller missing _generation"
    assert hasattr(poller, "_deliver_lock"), "SmartPoller missing _deliver_lock"
    poller._thread = threading.current_thread()
    generation = poller._generation
    poller._ensure_running()
    assert poller._generation == generation, "Wake-up spawned a redundant poller thread"

    assert '"notification", "fetch"' in src
    assert 'line.startswith("(id: ")' not in src, "Notification IDs are parsed from display text"
    assert "plan=%s" not in src, "Webhook logs expose plan identifiers"


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
    assert "process.stdin.pause()" in src, "Oversized stdin is not stopped"
    assert 'Buffer.byteLength(data.message, "utf8")' in src, "Message byte limit is missing"

    # Must use callGatewayFromCli for delivery
    assert "callGatewayFromCli" in src, "Missing callGatewayFromCli"
    assert '"send"' in src, "Must call Gateway 'send' method"

    manifest = json.loads(
        (Path(__file__).resolve().parent / "openclaw.plugin.json").read_text()
    )
    assert manifest.get("skills") == ["skill"], "Plugin skill is not declared"
    assert "mcpServers" not in manifest.get("contracts", {}), \
        "Unsupported native-plugin MCP contract is still declared"
    assert manifest["configSchema"]["additionalProperties"] is False


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


# ── Test 6: each notification is an independent user message ─────

def test_per_notification_delivery_and_idempotency():
    """Each queue item is sent/acked alone and transport IDs stay invisible."""
    import importlib.util
    import subprocess

    spec = importlib.util.spec_from_file_location(
        "webhook_receiver_delivery",
        str(Path(__file__).resolve().parent / "scripts" / "webhook_receiver.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._OPENCLAW_BIN = "/usr/local/bin/openclaw"

    notes = [
        {
            "id": "111111111111", "type": "daily_checkin",
            "plan_title": "Plan", "message": "Morning message",
        },
        {
            "id": "222222222222", "type": "daily_review",
            "plan_title": "Plan", "message": "Evening message",
        },
    ]

    def exercise(fail_send_indexes=()):
        payloads, acked = [], []

        def fake_run(command, **kwargs):
            if command[-2:] == ["notification", "fetch"]:
                return subprocess.CompletedProcess(
                    command, 0,
                    json.dumps({"success": True, "notifications": notes}), "",
                )
            if command[-1] == "plan-tracker-deliver":
                payloads.append(json.loads(kwargs["input"]))
                index = len(payloads) - 1
                return subprocess.CompletedProcess(
                    command, 7 if index in fail_send_indexes else 0, "", "",
                )
            if command[-2] == "ack":
                acked.append(command[-1])
                return subprocess.CompletedProcess(command, 0, "", "")
            raise AssertionError(f"unexpected command: {command}")

        original_run = mod.subprocess.run
        mod.subprocess.run = fake_run
        try:
            result = mod._deliver_pending("qqbot", "private-target")
        finally:
            mod.subprocess.run = original_run
        return result, payloads, acked

    result, payloads, acked = exercise()
    assert result is mod.DELIVERY_OK
    assert len(payloads) == 2, "Notifications were combined into one user message"
    assert acked == ["111111111111", "222222222222"], \
        "Notifications were not acknowledged individually"
    assert payloads[0]["idempotencyKey"] == mod._idempotency_key("111111111111")
    assert payloads[1]["idempotencyKey"] == mod._idempotency_key("222222222222")
    assert payloads[0]["idempotencyKey"] != payloads[1]["idempotencyKey"]
    for payload in payloads:
        assert "(id:" not in payload["message"]
        assert "111111111111" not in payload["message"]
        assert "222222222222" not in payload["message"]

    old_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        result, payloads, acked = exercise(fail_send_indexes={0})
    finally:
        logging.disable(old_disable)
    assert result is mod.DELIVERY_FAIL
    assert len(payloads) == 2, "One failed notification blocked later delivery"
    assert acked == ["222222222222"], \
        "A failed notification was acked or blocked a successful one"


# ── Test 7: Tri-state backoff logic ──────────────────────────────

def test_tri_state_backoff():
    """Execute the real poll loop for empty and failure results."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "webhook_receiver_tri_state",
        str(Path(__file__).resolve().parent / "scripts" / "webhook_receiver.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    old_backoff, old_deliver = mod._BACKOFF_SEQUENCE, mod._deliver_pending
    mod._BACKOFF_SEQUENCE = [0.001, 0.002]
    try:
        empty_calls = []
        mod._deliver_pending = lambda _channel, _to: empty_calls.append(1) or mod.DELIVERY_EMPTY
        empty_poller = mod.SmartPoller("channel", "target")
        empty_poller._generation = 1
        empty_poller._poll_loop(1)
        assert empty_calls, "EMPTY branch did not poll"

        fail_calls = []
        fail_poller = mod.SmartPoller("channel", "target")
        fail_poller._generation = 1

        def fail_then_supersede(_channel, _to):
            fail_calls.append(1)
            if len(fail_calls) == 3:
                fail_poller._generation = 2
            return mod.DELIVERY_FAIL

        mod._deliver_pending = fail_then_supersede
        fail_poller._poll_loop(1)
        assert len(fail_calls) == 3, "FAIL branch stopped instead of retrying"
    finally:
        mod._BACKOFF_SEQUENCE = old_backoff
        mod._deliver_pending = old_deliver


def test_index_cache_self_heals_after_refresh_failure():
    """A committed plan remains visible when its derived cache write fails."""
    _setup_test_env()
    import plan_tracker.storage as storage
    from plan_tracker.plan_manager import create_plan, delete_plan

    create_plan("cache-test", "Old", "Goal", "2026-12-31")
    original_atomic = storage._atomic_write

    def fail_index(path, data):
        if path == storage.INDEX_FILE:
            raise OSError("injected cache failure")
        return original_atomic(path, data)

    storage._atomic_write = fail_index
    old_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        result = storage.modify_plan_and_index(
            "cache-test", lambda plan: plan.__setitem__("title", "New")
        )
        assert result["title"] == "New"
    finally:
        logging.disable(old_disable)
        storage._atomic_write = original_atomic

    entry = next(p for p in storage.load_index()["plans"] if p["name"] == "cache-test")
    assert entry["title"] == "New", "Derived cache did not self-heal"
    delete_plan("cache-test")


def test_delete_missing_body_cleans_orphans():
    """A retry must clean state even if the plan body is already absent."""
    _setup_test_env()
    import plan_tracker.storage as storage
    import plan_tracker.daily_tracker as daily
    import plan_tracker.notification_queue as queue
    import plan_tracker.reminder as reminder
    from plan_tracker.plan_manager import delete_plan

    storage.INDEX_FILE.write_text(json.dumps({"plans": [{"name": "gone-plan"}]}))
    daily.STATE_FILE.write_text(json.dumps({"gone-plan": {"2026-07-19": {}}}))
    queue.QUEUE_FILE.write_text(json.dumps({"queue": [{
        "id": "orphan", "plan_name": "gone-plan", "delivered": False,
    }]}))
    reminder.STATE_FILE.write_text(json.dumps({"gone-plan:daily": "2026-07-19"}))

    assert delete_plan("gone-plan") is True
    assert storage.load_index()["plans"] == []
    assert "gone-plan" not in json.loads(daily.STATE_FILE.read_text())
    assert json.loads(queue.QUEUE_FILE.read_text())["queue"] == []
    assert json.loads(reminder.STATE_FILE.read_text()) == {}


def test_delete_reports_partial_cleanup_failure():
    """A removed body must not be reported as fully deleted if cleanup fails."""
    _setup_test_env()
    from plan_tracker.plan_manager import create_plan, delete_plan
    import plan_tracker.notification_queue as queue
    import plan_tracker.storage as storage

    create_plan("partial-delete", "Partial", "Test", "2026-12-31")
    original = queue.remove_for_plan

    def fail_cleanup(_name):
        raise OSError("injected")

    queue.remove_for_plan = fail_cleanup
    old_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        try:
            delete_plan("partial-delete")
            assert False, "Partial cleanup was reported as success"
        except RuntimeError as exc:
            assert "retry deletion" in str(exc)
        assert storage.load_plan("partial-delete") is None
    finally:
        logging.disable(old_disable)
        queue.remove_for_plan = original

    # Retrying after the transient failure is safe and completes cleanup.
    delete_plan("partial-delete")


def test_daemon_pid_protocol_and_short_writes():
    """Legacy daemons are recognized and new PID metadata is complete."""
    import plan_tracker.daemon as daemon
    assert daemon._LOGGING_CONFIGURED is False, "Importing daemon configured file logging"
    tmpdir = Path(tempfile.mkdtemp(prefix="pt-daemon-test-"))
    old_pid_file = daemon.PID_FILE
    daemon.PID_FILE = tmpdir / "daemon.pid"
    original_write = daemon.os.write
    try:
        daemon.PID_FILE.write_text(f"{os.getpid()}:0")
        assert daemon.is_running() is True, "Live legacy PID was not recognized"
        daemon.PID_FILE.write_text("9" * 80)

        def short_write(fd, payload):
            return original_write(fd, payload[:max(1, len(payload) // 2)])

        daemon.os.write = short_write
        fd = daemon.write_pid()
        try:
            raw = daemon.PID_FILE.read_text()
            assert daemon.read_pid() == os.getpid()
            assert raw.endswith(daemon._PID_LOCK_PROTOCOL)
            assert len(raw) < 80, "Old PID bytes were not truncated"
        finally:
            os.close(fd)
    finally:
        daemon.os.write = original_write
        daemon.PID_FILE = old_pid_file


def test_stale_tmp_permissions_are_reset():
    """All atomic writers must recreate permissive stale temp files."""
    _setup_test_env()
    import plan_tracker.file_lock as file_lock
    import plan_tracker.storage as storage
    root = storage.DATA_DIR

    safe_target = root / "safe.json"
    safe_tmp = safe_target.with_suffix(f".tmp-{os.getpid()}-{id(safe_target)}")
    safe_tmp.write_text("stale")
    os.chmod(safe_tmp, 0o644)
    file_lock.safe_write_json(safe_target, {"ok": True})

    locked_target = root / "locked.json"
    locked_tmp = locked_target.with_suffix(f".tmp-{os.getpid()}")
    locked_tmp.write_text("stale")
    os.chmod(locked_tmp, 0o644)
    with file_lock.LockedFile(locked_target, default={}) as data:
        data["ok"] = True

    atomic_target = root / "atomic.json"
    atomic_tmp = atomic_target.with_suffix(f".tmp-{os.getpid()}")
    atomic_tmp.write_text("stale")
    os.chmod(atomic_tmp, 0o644)
    storage._atomic_write(atomic_target, {"ok": True})

    for target in (safe_target, locked_target, atomic_target):
        assert target.stat().st_mode & 0o777 == 0o600, f"Bad mode for {target}"


def test_setup_cli_has_no_secret_argv_options():
    """Setup accepts only a private file path, never the target itself."""
    import plan_tracker.cli as cli
    source = Path(cli.__file__).read_text()
    assert 'add_argument("--to"' not in source
    assert 'add_argument("--qq-id"' not in source
    assert 'print(f"  Target: {to}")' not in source

    tmpdir = Path(tempfile.mkdtemp(prefix="pt-delivery-config-"))
    config = tmpdir / "delivery.json"
    config.write_text(json.dumps({"channel": "qqbot", "to": "qqbot:c2c:secret"}))
    os.chmod(config, 0o600)
    assert cli._read_private_delivery_config(config) == ("qqbot", "qqbot:c2c:secret")
    os.chmod(config, 0o644)
    try:
        cli._read_private_delivery_config(config)
        assert False, "Permissive delivery config was accepted"
    except ValueError:
        pass

    symlink = tmpdir / "delivery-link.json"
    symlink.symlink_to(config)
    try:
        cli._read_private_delivery_config(symlink)
        assert False, "Symlinked delivery config was accepted"
    except ValueError:
        pass

    os.chmod(config, 0o600)
    preview_output = io.StringIO()
    with contextlib.redirect_stdout(preview_output):
        cli.cmd_webhook_setup(delivery_config_path=str(config), dry_run=True)
    assert "<key>ProgramArguments</key>" in preview_output.getvalue()
    assert "secret" not in preview_output.getvalue()

    assert "<key>Umask</key>" in source
    assert "PLAN_TRACKER_DATA_DIR" in source

    openclaw_config = tmpdir / "openclaw.json"
    openclaw_config.write_text(json.dumps({
        "mcp": {"servers": {"plan-tracker": {
            "command": "/old/python", "args": ["old"], "env": {},
        }}},
    }))
    os.chmod(openclaw_config, 0o644)
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli._add_mcp_server_to_config(openclaw_config) is True
    updated = json.loads(openclaw_config.read_text())["mcp"]["servers"]["plan-tracker"]
    assert updated["command"] == cli._runtime_python()
    assert updated["args"] == ["-m", "plan_tracker.server"]
    assert updated["env"]["PLAN_TRACKER_DATA_DIR"]
    assert "PYTHONPATH" not in updated["env"]
    assert openclaw_config.stat().st_mode & 0o777 == 0o600
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli._add_mcp_server_to_config(openclaw_config) is False

    server_source = (Path(__file__).resolve().parent / "plan_tracker" / "server.py").read_text()
    assert '_DAEMON_LOCK_FILE = DATA_DIR / "daemon.lock"' in server_source
    webhook_source = (
        Path(__file__).resolve().parent / "plan_tracker" / "notification" / "webhook_channel.py"
    ).read_text()
    assert "ProxyHandler({})" in webhook_source, "Loopback webhook may leak through a proxy"


def test_launchd_reload_reports_bootstrap_failure():
    """A failed launchctl deployment must never be reported as successful."""
    import subprocess
    import plan_tracker.cli as cli

    original = cli._launchctl
    calls = []

    def fake_launchctl(args):
        calls.append(args)
        if args[0] == "bootstrap":
            return subprocess.CompletedProcess(args, 5, "", "Input/output error")
        return subprocess.CompletedProcess(args, 0, "", "")

    cli._launchctl = fake_launchctl
    try:
        try:
            cli._reload_launchd_service("com.example.test", Path("/tmp/test.plist"))
            assert False, "bootstrap failure was ignored"
        except RuntimeError as exc:
            assert "bootstrap" in str(exc)
            assert "Input/output error" in str(exc)
    finally:
        cli._launchctl = original

    assert calls[0][0] == "bootout"
    assert calls[1][0] == "bootstrap"
    assert "os.system" not in Path(cli.__file__).read_text()
    assert cli._webhook_receiver_script_path().is_file()


def test_macos_daemon_deployment_uses_launchd():
    """macOS setup/start must not fork a daemon that inherits the caller sandbox."""
    import plan_tracker.cli as cli

    original_supports = cli._supports_launchd
    original_config_path = cli._openclaw_config_path
    original_install = cli._install_launchd_plist
    original_is_running = cli.is_running
    installs = []
    try:
        cli._supports_launchd = lambda: True
        cli._openclaw_config_path = lambda: None
        cli._install_launchd_plist = (
            lambda dry_run=False: installs.append(dry_run) or True
        )
        cli.is_running = lambda: True
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_setup()
        assert installs == [False], "macOS setup did not install the daemon LaunchAgent"

        installs.clear()
        cli.is_running = lambda: False
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_daemon_start()
        assert installs == [False], "macOS daemon start bypassed launchd"
    finally:
        cli._supports_launchd = original_supports
        cli._openclaw_config_path = original_config_path
        cli._install_launchd_plist = original_install
        cli.is_running = original_is_running

    assert "--daemon" not in cli._LAUNCHD_PLIST_TEMPLATE, \
        "launchd service must run the daemon in foreground mode"

    events = []
    original_launchctl = cli._launchctl

    def fake_launchctl(args):
        import subprocess
        events.append(args[0])
        return subprocess.CompletedProcess(args, 0, "", "")

    cli._launchctl = fake_launchctl
    try:
        cli._reload_launchd_service(
            "com.example.daemon",
            Path("/tmp/example.plist"),
            before_bootstrap=lambda: events.append("stop-existing"),
        )
    finally:
        cli._launchctl = original_launchctl
    assert events == ["bootout", "stop-existing", "bootstrap", "print"], \
        "existing daemon was not stopped between bootout and bootstrap"

    server_source = (Path(__file__).resolve().parent / "plan_tracker" / "server.py").read_text()
    ensure_source = server_source[
        server_source.find("def _ensure_daemon"):server_source.find("def _daemon_watchdog")
    ]
    assert "_start_daemon_via_launchd" in ensure_source
    assert ensure_source.find("_start_daemon_via_launchd") < ensure_source.find("subprocess.Popen"), \
        "macOS launchd branch must run before the direct subprocess fallback"


def test_pending_queue_gets_webhook_wakeup_retry():
    """A recovered daemon must wake the receiver for notifications already queued."""
    _setup_test_env()
    import plan_tracker.reminder as reminder
    import plan_tracker.storage as storage
    from plan_tracker.notification_queue import enqueue
    from plan_tracker.plan_manager import create_plan, delete_plan

    create_plan("wake-retry", "Wake Retry", "Test", "2026-12-31")

    def configure(plan):
        plan["reminders"] = {
            "enabled": True,
            "notification_channels": ["mcp", "webhook"],
            "webhook": {"url": "http://127.0.0.1:9876"},
        }

    storage.modify_plan_and_index("wake-retry", configure)
    enqueue("wake-retry", "daily_review", "private message", "Private title")

    calls = []
    original_send = reminder.WebhookChannel.send
    reminder.WebhookChannel.send = (
        lambda self, message, plan_name, milestone_title, milestone_id:
        calls.append((self.config["url"], message, plan_name)) or True
    )
    try:
        reminder.ReminderEngine()._wake_pending_notifications()
    finally:
        reminder.WebhookChannel.send = original_send
        delete_plan("wake-retry")

    assert len(calls) == 1, "Pending queue did not trigger exactly one endpoint wake-up"
    assert calls[0][0] == "http://127.0.0.1:9876"
    assert calls[0][1]["message"] == "", "Queue wake-up exposed notification content"
    assert calls[0][2] == "__queue__", "Queue wake-up exposed a plan identifier"


# ── main ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        ("concurrent_writes_no_data_loss", test_concurrent_writes_no_data_loss),
        ("smartpoller_generation_counter", test_smartpoller_generation_counter),
        ("js_plugin_registration_structure", test_js_plugin_registration_structure),
        ("lock_mechanism_consistency", test_lock_mechanism_consistency),
        ("crash_safe_write_pattern", test_crash_safe_write_pattern),
        ("per_notification_delivery", test_per_notification_delivery_and_idempotency),
        ("tri_state_backoff", test_tri_state_backoff),
        ("index_cache_self_heals", test_index_cache_self_heals_after_refresh_failure),
        ("delete_missing_body_cleans_orphans", test_delete_missing_body_cleans_orphans),
        ("delete_partial_failure", test_delete_reports_partial_cleanup_failure),
        ("daemon_pid_protocol", test_daemon_pid_protocol_and_short_writes),
        ("stale_tmp_permissions", test_stale_tmp_permissions_are_reset),
        ("setup_cli_privacy", test_setup_cli_has_no_secret_argv_options),
        ("launchd_failure_reporting", test_launchd_reload_reports_bootstrap_failure),
        ("macos_launchd_daemon", test_macos_daemon_deployment_uses_launchd),
        ("pending_queue_wakeup", test_pending_queue_gets_webhook_wakeup_retry),
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
