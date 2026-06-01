"""Email notification channel (premium, requires API configuration).

Sends emails via the plan-tracker send-email REST API (mail.tempbox.cn).
"""

import json
import urllib.request
from datetime import datetime, timezone

from .base import NotificationChannel


DEFAULT_API_URL = "http://mail.tempbox.cn/api/send-email"
DEFAULT_API_KEY = "plan-tracker-api-key"

# Templates for different notification types
TPL_OVERDUE = """\
╔══════════════════════════════════════════════════╗
║           Plan Tracker — 里程碑已过期            ║
╚══════════════════════════════════════════════════╝

计划：{plan_title}
里程碑：{milestone_title}
目标日期：{target_date}
当前进度：{progress_pct}%

⚠ 该里程碑已超过目标日期 {days_overdue} 天，尚未完成。

建议：运行 plan-tracker check-in 更新实际进度，
或运行 plan-tracker update 重新评估计划时间线。

---
Plan Tracker · {send_time}
此邮件由计划跟踪系统自动发送，请勿回复。
"""

TPL_UPCOMING = """\
╔══════════════════════════════════════════════════╗
║           Plan Tracker — 里程碑即将到期          ║
╚══════════════════════════════════════════════════╝

计划：{plan_title}
里程碑：{milestone_title}
目标日期：{target_date}
剩余天数：{days_remaining} 天
当前进度：{progress_pct}%

📌 提醒：该里程碑将在 {days_remaining} 天后到期。

建议：评估当前进度是否能在目标日期前完成，
如进度落后可提前调整计划。

---
Plan Tracker · {send_time}
此邮件由计划跟踪系统自动发送，请勿回复。
"""

TPL_STALE = """\
╔══════════════════════════════════════════════════╗
║         Plan Tracker — 进度更新提醒              ║
╚══════════════════════════════════════════════════╝

计划：{plan_title}
里程碑：{milestone_title}
当前进度：{progress_pct}%

ℹ 你已经超过 7 天没有更新该里程碑的进度了。

建议：花几分钟记录最近的进展，
有助于准确追踪整体计划和及时发现问题。

---
Plan Tracker · {send_time}
此邮件由计划跟踪系统自动发送，请勿回复。
"""

TPL_WEEKLY = """\
╔══════════════════════════════════════════════════╗
║           Plan Tracker — 每周进度回顾            ║
╚══════════════════════════════════════════════════╝

计划：{plan_title}
目标：{goal}
总体进度：{progress_pct}%
已完成里程碑：{completed}/{total}
目标完成日期：{target_end_date}

📊 新的一周开始了，来看看计划的进展情况吧。

建议：回顾上周的成果，规划本周的目标。
运行 plan-tracker check-in 记录最新进度。

---
Plan Tracker · {send_time}
此邮件由计划跟踪系统自动发送，请勿回复。
"""

TPL_DAILY_CHECKIN = """\
╔══════════════════════════════════════════════════╗
║          Plan Tracker — 每日进度提醒             ║
╚══════════════════════════════════════════════════╝

计划：{plan_title}
目标：{goal}
目标完成日期：{target_end_date}
当前里程碑：{milestone_title}
当前进度：{progress_pct}%

☀ 新的一天开始了，祝你顺利完成今天的计划！

建议：花几分钟回顾今天的计划安排，
运行 plan-tracker check-in 记录最新进度。

---
Plan Tracker · {send_time}
此邮件由计划跟踪系统自动发送，请勿回复。
"""

TPL_DAILY_REVIEW = """\
╔══════════════════════════════════════════════════╗
║          Plan Tracker — 每日进度确认             ║
╚══════════════════════════════════════════════════╝

计划：{plan_title}
当前里程碑：{milestone_title}
当前进度：{progress_pct}%

🌙 今天的计划执行得如何？

请在 {timeout_minutes} 分钟内回复确认完成情况：
  ✅ 已完成 (completed)
  📌 部分完成 (partial)
  ❌ 未完成 (incomplete)

超时未确认将自动标记为「未完成」。

---
Plan Tracker · {send_time}
此邮件由计划跟踪系统自动发送，请勿回复。
"""

TPL_DAILY_TIMEOUT = """\
╔══════════════════════════════════════════════════╗
║          Plan Tracker — 超时通知                 ║
╚══════════════════════════════════════════════════╝

计划：{plan_title}

⏰ 今天的晚间确认已超时，系统已将计划自动标记为「未完成」。

如需补确认，请运行 daily_confirm 工具，
补确认将归档到明天的计划记录中。

---
Plan Tracker · {send_time}
此邮件由计划跟踪系统自动发送，请勿回复。
"""


class EmailChannel(NotificationChannel):
    """Email notification via plan-tracker email API (mail.tempbox.cn)."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def send(
        self,
        message: str,
        plan_name: str,
        milestone_title: str,
        milestone_id: str,
    ) -> bool:
        if not self.config or not self.config.get("enabled"):
            return False

        try:
            api_url = self.config.get("api_url", DEFAULT_API_URL)
            api_key = self.config.get("api_key", DEFAULT_API_KEY)
            recipient = self.config.get("recipient", "")

            if not recipient:
                return False

            send_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            # Build subject: [Plan Tracker] Plan Name — Milestone Title / Type
            plan_title = plan_name
            if isinstance(message, dict):
                plan_title = message.get("plan_title", plan_name)
                ntype = message.get("type", "")
                type_labels = {
                    "overdue": "里程碑已过期",
                    "upcoming": "里程碑即将到期",
                    "stale": "进度更新提醒",
                    "weekly": "每周进度回顾",
                    "daily_checkin": "每日进度提醒",
                    "daily_review": "每日进度确认",
                    "daily_timeout": "超时通知",
                }
                label = type_labels.get(ntype, "")
                if milestone_title:
                    subject = f"[Plan Tracker] {plan_title} — {milestone_title} ({label})"
                else:
                    subject = f"[Plan Tracker] {plan_title} — {label}"
            else:
                if milestone_title:
                    subject = f"[Plan Tracker] {plan_name} — {milestone_title}"
                else:
                    subject = f"[Plan Tracker] {plan_name}"

            # If the message already contains structured data, format it;
            # otherwise use the message as-is
            if isinstance(message, dict):
                body = self._format_notification(message, send_time)
            else:
                body = self._format_plain(message, plan_name, milestone_title, send_time)

            payload = json.dumps({
                "subject": subject,
                "body": body,
                "to_address": recipient,
            }).encode("utf-8")

            req = urllib.request.Request(
                api_url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": api_key,
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                return resp_data.get("status") == "ok"
        except Exception:
            return False

    def _format_notification(self, data: dict, send_time: str) -> str:
        """Format a structured notification dict into an email body."""
        ntype = data.get("type", "info")
        fmt = {
            **data,
            "send_time": send_time,
        }

        if ntype == "overdue":
            return TPL_OVERDUE.format(**fmt)
        elif ntype == "upcoming":
            return TPL_UPCOMING.format(**fmt)
        elif ntype == "stale":
            return TPL_STALE.format(**fmt)
        elif ntype == "weekly":
            return TPL_WEEKLY.format(**fmt)
        elif ntype == "daily_checkin":
            return TPL_DAILY_CHECKIN.format(**fmt)
        elif ntype == "daily_review":
            return TPL_DAILY_REVIEW.format(**fmt)
        elif ntype == "daily_timeout":
            return TPL_DAILY_TIMEOUT.format(**fmt)
        else:
            return self._format_plain(
                data.get("message", ""),
                data.get("plan_title", ""),
                data.get("milestone_title", ""),
                send_time,
            )

    def _format_plain(
        self,
        message: str,
        plan_name: str,
        milestone_title: str,
        send_time: str,
    ) -> str:
        """Format a plain message into an email body."""
        return (
            "╔══════════════════════════════════════════════════╗\n"
            "║              Plan Tracker — 计划提醒              ║\n"
            "╚══════════════════════════════════════════════════╝\n"
            f"\n计划：{plan_name}"
            f"\n里程碑：{milestone_title or '—'}"
            f"\n\n{message}\n"
            f"\n---"
            f"\nPlan Tracker · {send_time}"
            f"\n此邮件由计划跟踪系统自动发送，请勿回复。\n"
        )

    def is_available(self) -> bool:
        return bool(
            self.config
            and self.config.get("enabled")
            and self.config.get("recipient")
        )
