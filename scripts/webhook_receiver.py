#!/usr/bin/env python3
"""Webhook receiver for plan-tracker — real-time notification delivery.

Receives POST requests from the plan-tracker daemon's WebhookChannel,
runs ``plan-tracker.cli deliver`` to atomically fetch and ack pending
notifications, then delivers them through the privacy-safe OpenClaw plugin CLI.

Smart polling: when a webhook POST wakes the receiver, a background
poller checks the notification queue with exponential backoff.  If the
queue stays empty through the full backoff cycle the poller goes dormant,
eliminating unnecessary polling when there are no notifications.

Usage:
  python scripts/webhook_receiver.py [--host 127.0.0.1] [--port 9876]

Channel and target are read from webhook_delivery.json (set by webhook-setup).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import logging.handlers
import os
import re
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
logger = logging.getLogger("webhook-receiver")
_LOGGING_CONFIGURED = False


def _configure_logging() -> None:
    """Configure receiver logging only in the service process."""
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(_DATA_DIR, 0o700)
    # RotatingFileHandler and rotated logs are private from first creation.
    os.umask(0o077)
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
    try:
        os.chmod(_WEBHOOK_LOG, 0o600)
    except OSError:
        pass
    _LOGGING_CONFIGURED = True

# Resolve the openclaw binary (not the shell function)
def _resolve_openclaw_bin() -> str | None:
    """Find the openclaw binary, trying nvm current version first."""
    import shutil as _shutil

    # 1. Try the currently active nvm Node.js version
    _nvm_current = Path.home() / ".nvm" / "versions" / "node"
    if _nvm_current.is_dir():
        try:
            def _version_key(path: Path) -> tuple[int, ...]:
                return tuple(int(part) for part in re.findall(r"\d+", path.name))

            _versions = sorted(
                [d for d in _nvm_current.iterdir() if d.is_dir()],
                key=_version_key,
                reverse=True,
            )
            for _v in _versions:
                _candidate = _v / "bin" / "openclaw"
                if _candidate.is_file() and os.access(_candidate, os.X_OK):
                    return str(_candidate)
        except OSError:
            pass

    # 2. Fall back to common install locations
    for _candidate in (
        "/opt/homebrew/bin/openclaw",
        "/usr/local/bin/openclaw",
    ):
        if Path(_candidate).is_file() and os.access(_candidate, os.X_OK):
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
    """Load the delivery target through the CLI's private-file validator."""
    from plan_tracker.cli import _read_private_delivery_config
    try:
        channel, to = _read_private_delivery_config(_DELIVERY_CONFIG)
        return {"channel": channel, "to": to}
    except ValueError as exc:
        logger.error("Invalid delivery configuration: %s", exc)
        return {}

# ── Delivery ────────────────────────────────────────────────────


# ── Delivery result constants ──────────────────────────────────
DELIVERY_EMPTY = None       # nothing to deliver
DELIVERY_OK = True          # delivered + acked successfully
DELIVERY_FAIL = False       # fetch/send/ack failed, retry


def _idempotency_key(notification_id: str) -> str:
    """Return a stable, opaque key for exactly one queue notification."""
    return hashlib.sha256(notification_id.encode("ascii")).hexdigest()[:16]


def _deliver_pending(channel: str, to: str):
    """Fetch undelivered notifications, relay each one, and ack individually.

    Returns:
        DELIVERY_EMPTY (None): nothing pending.
        DELIVERY_OK (True):   delivered and acked.
        DELIVERY_FAIL (False): fetch/send/ack failed (retryable).
    """
    # Step 1: fetch without ack
    try:
        result = subprocess.run(
            [_PYTHON, "-m", "plan_tracker.cli", "notification", "fetch"],
            capture_output=True, text=True, timeout=15,
            cwd=str(_PKG_DIR),
        )
    except Exception:
        logger.exception("fetch command failed")
        return DELIVERY_FAIL

    if result.returncode != 0:
        logger.error("fetch command failed (rc=%d)", result.returncode)
        return DELIVERY_FAIL
    try:
        response = json.loads(result.stdout)
        pending = response.get("notifications", [])
        if not response.get("success") or not isinstance(pending, list):
            raise ValueError("invalid notification response")
    except (json.JSONDecodeError, AttributeError, ValueError):
        logger.error("fetch command returned invalid JSON")
        return DELIVERY_FAIL
    if not pending:
        return DELIVERY_EMPTY

    logger.info("Fetched %d notification(s)", len(pending))

    # Step 2: deliver via plan-tracker-deliver (stdin, no argv leak).
    if _OPENCLAW_BIN is None:
        logger.error("openclaw binary not found — cannot deliver")
        return DELIVERY_FAIL

    failed = False
    delivered_count = 0
    for note in pending:
        if not isinstance(note, dict) or not re.fullmatch(
            r"[0-9a-f]{12}", str(note.get("id", ""))
        ):
            logger.error("fetch command returned a malformed notification")
            failed = True
            continue

        note_id = str(note["id"])
        title = f"--- [{note.get('type', 'unknown')}] {note.get('plan_title', '')} ---"
        # Queue IDs are transport metadata.  Keep them out of user-visible
        # content; the opaque hash below is used only by the gateway.
        text = f"{title}\n{str(note.get('message', ''))}".rstrip()
        payload = json.dumps({
            "channel": channel,
            "target": to,
            "message": text,
            "idempotencyKey": _idempotency_key(note_id),
        })

        try:
            msg_result = subprocess.run(
                [_OPENCLAW_BIN, "plan-tracker-deliver"],
                input=payload, text=True, capture_output=True, timeout=15,
                env={**os.environ,
                     "PATH": f"{Path(_OPENCLAW_BIN).parent}:{os.environ.get('PATH', '')}"},
            )
        except Exception:
            logger.exception("delivery failed")
            failed = True
            continue

        if msg_result.returncode != 0:
            # Don't log stderr — it may contain message fragments.
            logger.error("delivery failed (rc=%d)", msg_result.returncode)
            failed = True
            continue

        # Step 3: ack this notification immediately.  A failure affects only
        # this item; later notifications still get their own delivery attempt.
        try:
            ack_result = subprocess.run(
                [_PYTHON, "-m", "plan_tracker.cli", "notification", "ack", note_id],
                capture_output=True, text=True, timeout=10,
                cwd=str(_PKG_DIR),
            )
            if ack_result.returncode == 0:
                delivered_count += 1
            else:
                logger.error(
                    "ack failed (rc=%d) — notification will be re-delivered",
                    ack_result.returncode,
                )
                failed = True
        except Exception:
            logger.exception("ack command failed — notification will be re-delivered")
            failed = True

    if delivered_count:
        logger.info("Delivered and acked %d notification(s) via %s", delivered_count, channel)
    return DELIVERY_FAIL if failed else DELIVERY_OK


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
        self._deliver_lock = threading.Lock()  # serializes delivery calls
        self._thread: threading.Thread | None = None
        self._generation = 0

    # ── public API ──────────────────────────────────────────────

    def wakeup(self) -> None:
        """Signal that a notification may be available."""
        self._wakeup.set()
        self._ensure_running()

    # ── internals ───────────────────────────────────────────────

    def _ensure_running(self) -> None:
        """Start one poller thread, or let the active thread handle the wake."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._generation += 1
            self._wakeup.clear()
            self._thread = threading.Thread(
                target=self._poll_loop,
                args=(self._generation,),
                daemon=True,
                name="plan-tracker-smart-poller",
            )
            self._thread.start()
            logger.info("Smart poller started gen=%d", self._generation)

    def _poll_loop(self, my_generation: int) -> None:
        """Background thread: poll, stop when idle. Exits if a newer
        generation takes over."""
        backoff_idx = 0
        empty_streak = 0

        while True:
            # ── Stale check ────────────────────────────────
            with self._lock:
                if self._generation != my_generation:
                    logger.debug("Smart poller gen=%d superseded", my_generation)
                    return

            # ── Check queue (serialized across threads) ────
            with self._deliver_lock:
                if self._generation != my_generation:
                    return
                result = _deliver_pending(self._channel, self._to)

            if result is DELIVERY_OK:
                backoff_idx = 0
                empty_streak = 0
            elif result is DELIVERY_EMPTY:
                # Queue empty — increase backoff toward dormant
                if backoff_idx < len(_BACKOFF_SEQUENCE) - 1:
                    backoff_idx += 1
                empty_streak += 1
            else:  # DELIVERY_FAIL — retry but don't stop
                # Keep backoff low so we keep retrying
                backoff_idx = max(0, backoff_idx - 1)
                empty_streak = max(0, empty_streak - 1)
                if backoff_idx < len(_BACKOFF_SEQUENCE) - 1:
                    backoff_idx += 1

            # ── Stop condition ─────────────────────────────
            if backoff_idx >= len(_BACKOFF_SEQUENCE) - 1 and empty_streak >= 1:
                with self._lock:
                    if self._generation != my_generation:
                        return
                    if self._wakeup.is_set():
                        self._wakeup.clear()
                        backoff_idx = 0
                        empty_streak = 0
                        continue
                    else:
                        self._thread = None
                        logger.info(
                            "Smart poller gen=%d: queue empty through full backoff — "
                            "going dormant", my_generation)
                        return

            # ── Wait ───────────────────────────────────────
            timeout = _BACKOFF_SEQUENCE[backoff_idx]
            self._wakeup.wait(timeout=timeout)
            self._wakeup.clear()


# ── HTTP server ──────────────────────────────────────────────────


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "plan-tracker-webhook/1"
    sys_version = ""
    channel: str = "qqbot"
    to: str = ""
    poller: SmartPoller | None = None

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self.send_error(400, "Invalid Content-Length")
            return
        if content_length < 0:
            self.send_error(400, "Invalid Content-Length")
            return
        # Limit body size to 64 KB
        if content_length > 65536:
            self.send_response(413)
            self.end_headers()
            return
        body = self.rfile.read(content_length) if content_length else b""

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return
        if not isinstance(data, dict):
            self.send_error(400, "JSON body must be an object")
            return

        # Do not log plan names or payload content; the POST is only a wake-up
        # signal and delivery reads the authoritative private queue.
        logger.info("Webhook wake-up received (%d bytes)", len(body))

        # Respond immediately so the daemon doesn't time out
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
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
    _configure_logging()
    parser = argparse.ArgumentParser(description="Plan Tracker webhook receiver")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (must be localhost)")
    parser.add_argument("--port", type=int, default=9876, help="Listen port")
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("Error: --host must be a localhost address", file=sys.stderr)
        sys.exit(1)
    if not 1 <= args.port <= 65535:
        print("Error: --port must be between 1 and 65535", file=sys.stderr)
        sys.exit(1)

    cfg = _load_delivery_config()
    channel = cfg.get("channel", "qqbot")
    to = cfg.get("to", "")

    if not to:
        print("Error: no delivery target configured. "
              "Run 'python -m plan_tracker.cli webhook-setup' to configure.",
              file=sys.stderr)
        sys.exit(1)

    # ── Wire up handler + poller ────────────────────────────────
    WebhookHandler.channel = channel
    WebhookHandler.to = to
    WebhookHandler.poller = SmartPoller(channel, to)

    logger.info("Running initial queue drain...")
    drain_result = _deliver_pending(channel, to)
    if drain_result is DELIVERY_OK:
        WebhookHandler.poller.wakeup()
    elif drain_result is DELIVERY_FAIL:
        logger.warning("Initial delivery failed — will retry")
        WebhookHandler.poller.wakeup()
    else:
        logger.info("Initial queue drain: nothing pending")

    server = HTTPServer((args.host, args.port), WebhookHandler)
    logger.info("Webhook receiver listening on http://%s:%d", args.host, args.port)
    logger.info("Delivery channel: %s", channel)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
