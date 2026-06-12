#!/usr/bin/env python3
"""Webhook receiver for plan-tracker — real-time notification delivery.

Receives POST requests from the plan-tracker daemon's WebhookChannel,
runs ``plan-tracker.cli deliver`` to atomically fetch and ack pending
notifications, then delivers them to the user via ``openclaw agent``.

Usage:
  python scripts/webhook_receiver.py [--host 127.0.0.1] [--port 9876]
                                     [--channel qqbot]
                                     [--to qqbot:c2c:<hex-id>]
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [webhook-receiver] %(levelname)s: %(message)s",
)
logger = logging.getLogger("webhook-receiver")

_PKG_DIR = Path(__file__).resolve().parent.parent
_PYTHON = sys.executable

# Resolve the openclaw binary (not the shell function)
_OPENCLAW_BIN = None
for _candidate in (
    "/Users/wzl/.nvm/versions/node/v25.8.0/bin/openclaw",
    "/opt/homebrew/bin/openclaw",
    "/usr/local/bin/openclaw",
):
    if Path(_candidate).exists():
        _OPENCLAW_BIN = _candidate
        break

if _OPENCLAW_BIN is None:
    # Fall back to PATH search
    import shutil as _shutil
    _found = _shutil.which("openclaw")
    if _found:
        _OPENCLAW_BIN = _found

# ── Delivery ────────────────────────────────────────────────────


def fetch_and_deliver(channel: str, to: str) -> bool:
    """Run the deliver command, then push output to the user.

    1. ``plan-tracker.cli deliver`` — atomically fetch + ack pending
    2. If output is non-empty, relay it via ``openclaw agent --deliver``

    Returns True if a notification was delivered.
    """
    # Step 1: fetch + ack
    try:
        result = subprocess.run(
            [_PYTHON, "-m", "plan_tracker.cli", "deliver"],
            capture_output=True, text=True, timeout=15,
            cwd=str(_PKG_DIR),
        )
    except Exception:
        logger.exception("deliver command failed")
        return False

    text = result.stdout.strip()
    if not text:
        return False  # nothing to deliver

    logger.info("Fetched %d chars of notification text", len(text))

    # Step 2: push to user via openclaw agent relay
    relay_prompt = (
        "你的唯一任务是将以下内容原样发送给用户。"
        "不要添加任何问候、解释、建议或额外内容。"
        "不要改变格式。直接输出以下内容：\n\n" + text
    )

    try:
        agent_result = subprocess.run(
            [
                _OPENCLAW_BIN, "agent",
                "--session-key", "agent:main:plan-tracker-delivery",
                "--message", relay_prompt,
                "--deliver",
                "--channel", channel,
                "--to", to,
                "--timeout", "30",
                "--thinking", "off",
            ],
            capture_output=True, text=True, timeout=45,
            env={**__import__("os").environ,
                 "PATH": "/Users/wzl/.nvm/versions/node/v25.8.0/bin:" + __import__("os").environ.get("PATH", "")},
        )
        if agent_result.returncode == 0:
            logger.info("Delivered to %s via %s", to, channel)
            return True
        else:
            logger.error("openclaw agent failed (rc=%d): %s",
                         agent_result.returncode, agent_result.stderr.strip())
            return False
    except Exception:
        logger.exception("openclaw agent call failed")
        return False


# ── HTTP server ──────────────────────────────────────────────────


class WebhookHandler(BaseHTTPRequestHandler):
    channel: str = "qqbot"
    to: str = ""

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        ntype = data.get("type", "unknown")
        plan = data.get("plan_title", data.get("plan_name", "unknown"))
        logger.info("Webhook received: type=%s plan=%s", ntype, plan)

        ok = fetch_and_deliver(self.channel, self.to)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "status": "ok" if ok else "empty",
            "delivered": ok,
        }).encode())

    def log_message(self, format, *args):
        logger.debug("%s - %s", self.client_address[0], format % args)


def main():
    parser = argparse.ArgumentParser(description="Plan Tracker webhook receiver")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=9876, help="Listen port")
    parser.add_argument("--channel", default="qqbot",
                        help="OpenClaw delivery channel (default: qqbot)")
    parser.add_argument("--to", default="",
                        help="Delivery target, e.g. qqbot:c2c:<hex-id>")
    args = parser.parse_args()

    if not args.to:
        parser.error("--to is required (e.g. qqbot:c2c:<hex-id>)")

    # Inject config into handler class
    WebhookHandler.channel = args.channel
    WebhookHandler.to = args.to

    server = HTTPServer((args.host, args.port), WebhookHandler)
    logger.info("Webhook receiver listening on http://%s:%d", args.host, args.port)
    logger.info("Delivery: %s → %s", args.channel, args.to)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
