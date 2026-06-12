#!/usr/bin/env python3
"""Webhook receiver for plan-tracker — real-time notification delivery.

Receives POST requests from the plan-tracker daemon's WebhookChannel,
runs ``plan-tracker.cli deliver`` to atomically fetch and ack pending
notifications, and prints them to stdout for delivery via OpenClaw.

Usage:
  python scripts/webhook_receiver.py [--host 127.0.0.1] [--port 9876]

When used with OpenClaw, configure a cron job with ``sessionTarget: isolated``
and ``wakeMode: now`` that runs this process.  Each POST triggers an immediate
run of the deliver command — the output is the user-facing notification text.
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

# Path to the plan-tracker CLI (auto-detect)
_PKG_DIR = Path(__file__).resolve().parent.parent
_PYTHON = sys.executable
_CLI_ARGS = ["-m", "plan_tracker.cli", "deliver"]


def fetch_and_deliver() -> str | None:
    """Run the deliver command and return its output, or None if empty."""
    try:
        result = subprocess.run(
            [_PYTHON, *_CLI_ARGS],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(_PKG_DIR),
        )
        output = result.stdout.strip()
        if output:
            logger.info("Delivered %d chars of notification text", len(output))
            return output
        return None
    except Exception:
        logger.exception("Failed to run deliver command")
        return None


class WebhookHandler(BaseHTTPRequestHandler):
    """Handle incoming webhook POSTs."""

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

        # Run deliver to atomically fetch + ack pending notifications
        output = fetch_and_deliver()
        if output:
            # Print to stdout — OpenClaw captures this for delivery
            print(output, flush=True)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok"}).encode())

    def log_message(self, format, *args):
        logger.debug("%s - %s", self.client_address[0], format % args)


def main():
    parser = argparse.ArgumentParser(description="Plan Tracker webhook receiver")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=9876, help="Listen port")
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), WebhookHandler)
    logger.info("Webhook receiver listening on http://%s:%d", args.host, args.port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
