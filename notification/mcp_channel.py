"""MCP protocol notification channel (free, default).

Sends notifications to the MCP client via stderr/logging.
These are picked up by OpenClaw and shown to the user.
"""

import json
import sys

from .base import NotificationChannel


class McpChannel(NotificationChannel):
    """Notification via MCP protocol logging."""

    def send(
        self,
        message: str,
        plan_name: str,
        milestone_title: str,
        milestone_id: str,
    ) -> bool:
        payload = {
            "level": "notice",
            "data": {
                "type": "plan_tracker_reminder",
                "plan_name": plan_name,
                "milestone_id": milestone_id,
                "milestone_title": milestone_title,
                "message": message,
            },
        }
        # MCP servers communicate via stderr for logging/notifications
        sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stderr.flush()
        return True

    def is_available(self) -> bool:
        return True  # Always available
