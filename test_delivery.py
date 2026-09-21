"""Delivery regressions: isolated child processes; no gateway or real messages."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch


def receiver():
    spec = importlib.util.spec_from_file_location(
        "delivery_test_receiver", Path(__file__).parent / "scripts/webhook_receiver.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DeliveryTests(unittest.TestCase):
    def test_process_tree_and_scratch_are_reclaimed(self):
        mod = receiver()
        mod._DELIVERY_TIMEOUT = 0.5
        mod._PROCESS_EXIT_GRACE = 0.1
        with tempfile.TemporaryDirectory() as root:
            launcher = Path(root) / "fake-openclaw"
            launcher.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, signal, subprocess, sys, time
payload = json.load(sys.stdin)
scratch = pathlib.Path(os.environ["TMPDIR"])
(scratch / "openclaw-plugin-build-test").mkdir()
(scratch / "openclaw-plugin-build-test" / "dependency").write_bytes(b"x" * 4096)
child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
pathlib.Path(payload["record"]).write_text(json.dumps({"scratch": str(scratch), "group": os.getpid()}))
time.sleep(0.15)
if payload["mode"] == "timeout":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
sys.exit(0 if payload["mode"] == "success" else 7)
''')
            launcher.chmod(0o700)
            mod._OPENCLAW_BIN = str(launcher)
            for mode in ("timeout", "failure", "success"):
                with self.subTest(mode=mode):
                    record = Path(root) / "record.json"
                    payload = json.dumps({"mode": mode, "record": str(record)})
                    if mode == "timeout":
                        with self.assertRaises(subprocess.TimeoutExpired):
                            mod._run_delivery(payload)
                    else:
                        result = mod._run_delivery(payload)
                        self.assertEqual(result.returncode, 0 if mode == "success" else 7)
                    observed = json.loads(record.read_text())
                    self.assertFalse(Path(observed["scratch"]).exists())
                    deadline = time.monotonic() + 3
                    while True:
                        try:
                            os.killpg(observed["group"], 0)
                        except ProcessLookupError:
                            break
                        if time.monotonic() >= deadline:
                            self.fail("delivery process group survived cleanup")
                        time.sleep(0.05)

    def test_spawn_failure_cleans_scratch(self):
        mod = receiver()
        mod._OPENCLAW_BIN = "/nonexistent/plan-tracker-test-openclaw"
        with tempfile.TemporaryDirectory() as root:
            original = tempfile.TemporaryDirectory
            with patch.object(mod.tempfile, "TemporaryDirectory",
                              side_effect=lambda **kw: original(dir=root, **kw)):
                with self.assertRaises(FileNotFoundError):
                    mod._run_delivery("{}")
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_low_disk_does_not_spawn(self):
        mod = receiver()
        with patch.object(mod.shutil, "disk_usage", return_value=types.SimpleNamespace(free=0)), \
                patch.object(mod.subprocess, "Popen") as spawn:
            with self.assertRaises(OSError):
                mod._run_delivery("{}")
            spawn.assert_not_called()

    def test_webhooks_cannot_bypass_backoff_and_success_resets_it(self):
        mod = receiver()
        poller = mod.SmartPoller("channel", "target")
        poller._generation = 1
        clock, calls = [0.0], []
        results = [mod.DELIVERY_FAIL] * 8 + [mod.DELIVERY_OK, mod.DELIVERY_FAIL]

        class WebhookStorm:
            def clear(self):
                pass

            def wait(self, timeout):
                clock[0] += min(1, timeout)
                return True

        def deliver(*args):
            calls.append(clock[0])
            if results:
                return results.pop(0)
            poller._generation += 1
            return mod.DELIVERY_FAIL

        poller._wakeup = WebhookStorm()
        mod.time = types.SimpleNamespace(monotonic=lambda: clock[0])
        mod._deliver_pending = deliver
        poller._poll_loop(1)
        waits = [b - a for a, b in zip(calls, calls[1:])]
        self.assertEqual(waits[:8], [30, 60, 120, 240, 480, 600, 3600, 3600])
        self.assertEqual(waits[-1], 30)

    def test_cli_error_returns_to_host_cleanup(self):
        script = '''
const plugin = require('./src/index.js');
let action;
const command = { command() { return this; }, description() { return this; },
  action(fn) { action = fn; return this; } };
plugin.register({registerCli(register) { register({program: command}); }});
action().then(() => console.log('HOST_CLEANUP_REACHED'));
'''
        result = subprocess.run(["node", "-e", script], input="{}", text=True,
                                capture_output=True, cwd=Path(__file__).parent, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("HOST_CLEANUP_REACHED", result.stdout)
        manifest = json.loads((Path(__file__).parent / "openclaw.plugin.json").read_text())
        self.assertIn("plan-tracker-deliver", manifest["activation"]["onCommands"])


if __name__ == "__main__":
    unittest.main()
