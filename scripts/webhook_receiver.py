#!/usr/bin/env python3
"""Webhook receiver for plan-tracker — real-time notification delivery.

Receives POST requests from the plan-tracker daemon's WebhookChannel,
runs ``plan-tracker.cli deliver`` to atomically fetch and ack pending
notifications, then delivers them to the user via ``openclaw agent``.

Smart polling: when a webhook POST wakes the receiver, a background
poller checks the notification queue with exponential backoff.  If the
queue stays empty through the full backoff cycle the poller goes dormant,
eliminating unnecessary polling when there are no notifications.

Usage:
  python scripts/webhook_receiver.py [--host 127.0.0.1] [--port 9876]
                                     [--channel qqbot]
                                     [--to qqbot:c2c:<hex-id>]
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent
_PYTHON = sys.executable
# Use PLAN_TRACKER_DATA_DIR if set, otherwise fall back to source-relative path
_DATA_DIR = Path(os.environ.get("PLAN_TRACKER_DATA_DIR", _PKG_DIR / "data"))
_DELIVERY_CONFIG = _DATA_DIR / "webhook_delivery.json"
_WEBHOOK_LOG = _DATA_DIR / "webhook-stderr.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [webhook-receiver] %(levelname)s: %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            _WEBHOOK_LOG, maxBytes=1_048_576, backupCount=3,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("webhook-receiver")

# Resolve the openclaw binary (not the shell function)
def _resolve_openclaw_bin() -> str | None:
    """Find the openclaw binary, trying nvm current version first."""
    import shutil as _shutil

    # 1. Try the currently active nvm Node.js version
    _nvm_current = Path.home() / ".nvm" / "versions" / "node"
    if _nvm_current.is_dir():
        try:
            _versions = sorted(
                [d for d in _nvm_current.iterdir() if d.is_dir()],
                reverse=True,  # newest first
            )
            for _v in _versions:
                _candidate = _v / "bin" / "openclaw"
                if _candidate.exists():
                    return str(_candidate)
        except OSError:
            pass

    # 2. Fall back to common install locations
    for _candidate in (
        "/opt/homebrew/bin/openclaw",
        "/usr/local/bin/openclaw",
    ):
        if Path(_candidate).exists():
            return _candidate

    # 3. Search PATH
    _found = _shutil.which("openclaw")
    if _found:
        return _found

    return None

_OPENCLAW_BIN = _resolve_openclaw_bin()


# ── Exponential backoff sequence (seconds) ─────────────────────────
# After each empty poll the backoff doubles.  When the last level
# returns empty the poller goes dormant until the next webhook POST.
_BACKOFF_SEQUENCE = [30, 60, 120, 240, 480, 600]


def _load_delivery_config() -> dict:
    """Load delivery config from disk, with CLI-arg overrides."""
    if _DELIVERY_CONFIG.exists():
        try:
            with open(_DELIVERY_CONFIG, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}

# ── Delivery ────────────────────────────────────────────────────


def _deliver_pending(channel: str, to: str) -> bool:
    """Fetch undelivered notifications, relay to user, and ack only on success.

    Uses a two-phase approach:
    1. ``notification fetch`` — read without ack
    2. Deliver via openclaw agent
    3. On success: ``notification ack`` — commit the delivery

    This prevents data loss: if delivery fails the notification stays
    in the queue for retry.
    """
    # Step 1: fetch with --no-ack (print only, don't mark as delivered)
    try:
        result = subprocess.run(
            [_PYTHON, "-m", "plan_tracker.cli", "deliver", "--no-ack"],
            capture_output=True, text=True, timeout=15,
            cwd=str(_PKG_DIR),
        )
    except Exception:
        logger.exception("fetch command failed")
        return False

    text = result.stdout.strip()
    if not text:
        return False

    # Parse notification IDs from the output
    ids = []
    for line in text.split("\n"):
        if line.startswith("(id: ") and line.endswith(")"):
            ids.append(line[5:-1])

    if not ids:
        return False

    logger.info("Fetched %d notification(s), %d chars", len(ids), len(text))

    # Step 2: deliver directly via openclaw message send (no LLM)
    if _OPENCLAW_BIN is None:
        logger.error("openclaw binary not found — cannot deliver")
        return False

    delivered = False
    try:
        msg_result = subprocess.run(
            [
                _OPENCLAW_BIN, "message", "send",
                "--channel", channel,
                "--target", to,
                "--message", text,
                "--json",
            ],
            capture_output=True, text=True, timeout=15,
            env={**__import__("os").environ,
                 "PATH": f"{Path(_OPENCLAW_BIN).parent}:{__import__('os').environ.get('PATH', '')}"},
        )
        if msg_result.returncode == 0:
            logger.info("Delivered to %s via %s", to, channel)
            delivered = True
        else:
            logger.error("openclaw message send failed (rc=%d): %s",
                         msg_result.returncode, msg_result.stderr.strip())
    except Exception:
        logger.exception("openclaw message send failed")

    # Step 3: ack only on success
    if delivered and ids:
        try:
            subprocess.run(
                [_PYTHON, "-m", "plan_tracker.cli", "notification", "ack"] + ids,
                capture_output=True, text=True, timeout=10,
                cwd=str(_PKG_DIR),
            )
            logger.info("Acked %d notification(s)", len(ids))
        except Exception:
            logger.exception("ack command failed — notifications will be re-delivered")

    return delivered


# ── Smart Poller ─────────────────────────────────────────────────


class SmartPoller:
    """Event-driven poller with exponential backoff and auto-sleep.

    Lifecycle
    ---------
    1. **Dormant** — no thread running, zero resource usage.
    2. **Wake-up** — ``wakeup()`` is called (by a webhook POST).
       The poller starts a background thread that immediately checks
       the queue.
    3. **Backoff** — after each empty poll the wait interval doubles
       (30 s → 60 s → 120 s → 240 s → 480 s → 600 s).  A non-empty
       poll resets the backoff to the start.
    4. **Dormant again** — when the queue has been empty through the
       full backoff cycle the poller stops its thread and returns to
       step 1, waiting for the next webhook POST.
    """

    def __init__(self, channel: str, to: str):
        self._channel = channel
        self._to = to
        self._wakeup = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False

    # ── public API ──────────────────────────────────────────────

    def wakeup(self) -> None:
        """Signal that a notification may be available.

        Safe to call from any thread.  Restarts the poller if it was
        dormant.
        """
        self._wakeup.set()
        self._ensure_running()

    # ── internals ───────────────────────────────────────────────

    def _ensure_running(self) -> None:
        """Start the poller thread if it isn't already alive."""
        with self._lock:
            if self._running and self._thread and self._thread.is_alive():
                return
            self._running = True
            self._wakeup.clear()
            self._thread = threading.Thread(
                target=self._poll_loop,
                daemon=True,
                name="plan-tracker-smart-poller",
            )
            self._thread.start()
            logger.info("Smart poller started (backoff: %s)",
                        " → ".join(f"{s}s" for s in _BACKOFF_SEQUENCE))

    def _poll_loop(self) -> None:
        """Background thread: poll with exponential backoff, stop when idle."""
        backoff_idx = 0
        empty_streak = 0

        while self._running:
            # ── Check queue now ─────────────────────────────────
            delivered = _deliver_pending(self._channel, self._to)

            if delivered:
                backoff_idx = 0
                empty_streak = 0
                logger.debug("Smart poller: delivered, backoff reset")
            else:
                empty_streak += 1
                if backoff_idx < len(_BACKOFF_SEQUENCE) - 1:
                    backoff_idx += 1
                logger.debug("Smart poller: empty streak=%d, backoff_idx=%d",
                             empty_streak, backoff_idx)

            # ── Stop condition ──────────────────────────────────
            # Go dormant when we've reached max backoff AND had at
            # least one empty poll at that level.
            if backoff_idx >= len(_BACKOFF_SEQUENCE) - 1 and empty_streak >= 1:
                logger.info(
                    "Smart poller: queue empty through full backoff cycle "
                    "(max %ds) — going dormant", _BACKOFF_SEQUENCE[-1]
                )
                self._running = False
                break

            # ── Wait (interruptible by wakeup) ──────────────────
            timeout = _BACKOFF_SEQUENCE[backoff_idx]
            self._wakeup.wait(timeout=timeout)
            self._wakeup.clear()

        self._thread = None  # Signal that we've exited so _ensure_running can restart
        logger.info("Smart poller stopped (dormant)")


# ── HTTP server ──────────────────────────────────────────────────


class WebhookHandler(BaseHTTPRequestHandler):
    channel: str = "qqbot"
    to: str = ""
    poller: SmartPoller | None = None

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        # Limit body size to 64 KB
        if content_length > 65536:
            self.send_response(413)
            self.end_headers()
            return
        body = self.rfile.read(content_length) if content_length else b""

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        ntype = data.get("type", "unknown")
        plan = data.get("plan_title", data.get("plan_name", "unknown"))
        logger.info("Webhook received: type=%s plan=%s", ntype, plan)

        # Respond immediately so the daemon doesn't time out
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "accepted"}).encode())

        # Wake the SmartPoller — its first check is immediate (before
        # any backoff sleep), giving sub-second delivery latency.
        # We intentionally do NOT spawn a separate immediate-delivery
        # thread here; doing so races with the SmartPoller's first
        # check and produces duplicate QQ messages.
        if self.poller is not None:
            self.poller.wakeup()

    def log_message(self, format, *args):
        logger.debug("%s - %s", self.client_address[0], format % args)


def main():
    parser = argparse.ArgumentParser(description="Plan Tracker webhook receiver")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (must be localhost)")
    parser.add_argument("--port", type=int, default=9876, help="Listen port")
    parser.add_argument("--channel", default="",
                        help="OpenClaw delivery channel (auto-detected if not set)")
    parser.add_argument("--to", default="",
                        help="Delivery target (auto-detected if not set)")
    args = parser.parse_args()

    # Reject non-localhost binding for security
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("Error: --host must be a localhost address (127.0.0.1, localhost, ::1)", file=sys.stderr)
        sys.exit(1)

    # Load delivery config from file (set by webhook-setup)
    cfg = _load_delivery_config()

    channel = args.channel or cfg.get("channel", "qqbot")
    to = args.to or cfg.get("to", "")

    if not to:
        print("Error: --to is required (e.g. qqbot:c2c:<hex-id>). "
              "Run 'python -m plan_tracker.cli webhook-setup' to configure.",
              file=sys.stderr)
        sys.exit(1)

    # ── Wire up handler + poller ────────────────────────────────
    WebhookHandler.channel = channel
    WebhookHandler.to = to
    WebhookHandler.poller = SmartPoller(channel, to)

    # Also do an initial queue drain in case notifications piled up
    # while the receiver was down (e.g. Gateway restart).
    logger.info("Running initial queue drain...")
    if _deliver_pending(channel, to):
        # Queue wasn't empty — start the poller for follow-up checks
        WebhookHandler.poller.wakeup()
    else:
        logger.info("Initial queue drain: nothing pending")

    server = HTTPServer((args.host, args.port), WebhookHandler)
    logger.info("Webhook receiver listening on http://%s:%d", args.host, args.port)
    logger.info("Delivery: %s → %s", channel, to)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
