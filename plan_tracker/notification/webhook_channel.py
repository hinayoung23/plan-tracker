"""Webhook notification channel.

POSTs notification data to a configured URL when a reminder fires.
Used for real-time push delivery instead of polling the queue.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone

from .base import NotificationChannel

logger = logging.getLogger("plan_tracker.webhook_channel")


class WebhookChannel(NotificationChannel):
    """Deliver notifications by POSTing to a webhook URL."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def send(
        self,
        message: str,
        plan_name: str,
        milestone_title: str,
        milestone_id: str,
    ) -> bool:
        url = self.config.get("url", "")
        if not url:
            logger.warning("Webhook channel has no URL configured, skipping")
            return False

        try:
            # message can be a dict (full notification) or a string
            if isinstance(message, dict):
                payload = {
                    "type": message.get("type", "info"),
                    "plan_name": plan_name,
                    "plan_title": message.get("plan_title", plan_name),
                    "milestone_title": milestone_title,
                    "milestone_id": milestone_id,
                    "message": message.get("message", ""),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            else:
                payload = {
                    "type": "info",
                    "plan_name": plan_name,
                    "plan_title": plan_name,
                    "milestone_title": milestone_title,
                    "milestone_id": milestone_id,
                    "message": str(message),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=5) as resp:
                if 200 <= resp.status < 300:
                    logger.info("Webhook sent: type=%s plan=%s", payload["type"], plan_name)
                    return True
                logger.warning("Webhook returned status %d", resp.status)
                return False
        except Exception:
            logger.exception("Webhook delivery failed")
            return False

    def is_available(self) -> bool:
        return bool(self.config.get("url", ""))
