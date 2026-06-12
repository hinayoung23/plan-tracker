"""Email notification channel.

Sends emails via the mail.tempbox.cn REST API with HMAC-SHA256
request signing for authentication and anti-replay protection.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import urllib.request
from datetime import datetime, timezone

from .base import NotificationChannel

DEFAULT_API_URL = "https://mail.tempbox.cn/api/send-email"

# ── Rate-limit / auth error messages from the server ──────────────

_RATE_LIMIT_CODES: dict[int, str] = {
    402: "rate limited",
}


def _build_signature(
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body_sha256: str,
    api_secret: str,
) -> str:
    """Build an HMAC-SHA256 signature for a mail.tempbox.cn request.

    Signing payload (lines joined by ``\\n``)::

        {METHOD}
        {PATH}
        {X-Timestamp}
        {X-Nonce}
        {SHA256(request_body)}
    """
    payload = "\n".join([method, path, timestamp, nonce, body_sha256])
    mac = hmac.new(
        api_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    )
    return mac.hexdigest()


# ── Email body templates ──────────────────────────────────────────

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
    """Email notification via mail.tempbox.cn with HMAC-SHA256 signing."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    # ── NotificationChannel interface ──────────────────────────

    def send(
        self,
        message: str,
        plan_name: str,
        milestone_title: str,
        milestone_id: str,
    ) -> bool:
        """Send an email notification. Returns True on success."""
        api_key_id = self.config.get("api_key_id", "")
        api_secret = self.config.get("api_secret", "")
        recipient = self.config.get("recipient", "")
        api_url = self.config.get("api_url", DEFAULT_API_URL)

        if not api_key_id or not api_secret or not recipient:
            return False

        # Build subject & body
        plan_title = plan_name
        send_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        if isinstance(message, dict):
            plan_title = message.get("plan_title", plan_name)
            body = self._format_notification(message, send_time)
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
            body = self._format_plain(str(message), plan_name, milestone_title, send_time)
            if milestone_title:
                subject = f"[Plan Tracker] {plan_name} — {milestone_title}"
            else:
                subject = f"[Plan Tracker] {plan_name}"

        # Build request body
        request_body = json.dumps({
            "to_address": recipient,
            "subject": subject,
            "body": body,
        }, ensure_ascii=False)
        body_bytes = request_body.encode("utf-8")

        # HMAC signing
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)  # 32 hex chars
        body_sha256 = hashlib.sha256(body_bytes).hexdigest()

        # Extract path from URL for signing
        from urllib.parse import urlparse
        parsed = urlparse(api_url)
        path = parsed.path or "/api/send-email"

        signature = _build_signature(
            method="POST",
            path=path,
            timestamp=timestamp,
            nonce=nonce,
            body_sha256=body_sha256,
            api_secret=api_secret,
        )

        try:
            req = urllib.request.Request(
                api_url,
                data=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key-Id": api_key_id,
                    "X-Timestamp": timestamp,
                    "X-Nonce": nonce,
                    "X-Signature": signature,
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                status = resp_data.get("status", "")
                return status == "ok"

        except urllib.error.HTTPError as exc:
            status = exc.code
            body_text = ""
            try:
                body_text = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass

            if status == 402:
                if "minute" in body_text.lower() or "min" in body_text.lower():
                    print(f"[plan-tracker] Email rate limit: minute cap exceeded for {recipient}")
                elif "day" in body_text.lower() or "daily" in body_text.lower():
                    print(f"[plan-tracker] Email rate limit: daily quota exceeded for {recipient}")
                elif "month" in body_text.lower() or "monthly" in body_text.lower():
                    print(f"[plan-tracker] Email rate limit: monthly quota exceeded for {recipient}")
                else:
                    print(f"[plan-tracker] Email rate limited (402) for {recipient}")
            elif status == 401:
                print(f"[plan-tracker] Email auth failed (401): {body_text.strip()}")
            else:
                print(f"[plan-tracker] Email API error {status}: {body_text.strip()}")
            return False

        except Exception:
            return False

    def is_available(self) -> bool:
        return bool(
            self.config.get("api_key_id")
            and self.config.get("api_secret")
            and self.config.get("recipient")
        )

    # ── Body formatting ────────────────────────────────────────

    def _format_notification(self, data: dict, send_time: str) -> str:
        """Format a structured notification dict into an email body."""
        ntype = data.get("type", "info")
        fmt = {**data, "send_time": send_time}

        tpl_map = {
            "overdue": TPL_OVERDUE,
            "upcoming": TPL_UPCOMING,
            "stale": TPL_STALE,
            "weekly": TPL_WEEKLY,
            "daily_checkin": TPL_DAILY_CHECKIN,
            "daily_review": TPL_DAILY_REVIEW,
            "daily_timeout": TPL_DAILY_TIMEOUT,
        }
        tpl = tpl_map.get(ntype)
        if tpl:
            try:
                return tpl.format(**fmt)
            except KeyError:
                pass
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
