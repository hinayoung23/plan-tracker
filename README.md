# Plan Tracker MCP

[English](#english) | [中文](#chinese)

---

<a id="english"></a>
## English

### Overview

**Plan Tracker** is an MCP (Model Context Protocol) server that gives AI assistants long-term plan management capabilities. It supports milestone tracking, progress check-ins, plan analysis, and scheduled reminders with **real-time webhook push delivery**.

Whether it's a learning roadmap, project plan, fitness goal, or reading list, Plan Tracker helps break big goals into executable milestones and continuously tracks progress.

### Features

- **Plan CRUD** — Create, view, update, delete plans with categories (learning/project/fitness/reading/custom)
- **Milestones** — Add and update milestones, view current and upcoming ones
- **Check-ins** — Record progress percentage, time spent, notes, blockers, and morale for each milestone
- **Analysis** — Progress deviation, pace ratio, remaining effort estimates, morale trends
- **Daily Reminders** — Morning check-in (default 08:30) + evening review (default 21:30) with 10-min auto-timeout
- **Milestone Reminders** — Event-scheduled engine fires reminders at exact configured times, with startup catch-up for missed reminders
- **Auto-start Daemon** — MCP Server auto-starts the daemon on first use with a watchdog thread
- **Notification delivery** — Webhook real-time push + queue fallback; auto-detects delivery channel; supports QQ/Telegram/Slack etc.

### Requirements

- Python >= 3.12
- MCP >= 1.0.0

### Installation

#### Option 1: OpenClaw Plugin Marketplace (recommended)

```bash
openclaw plugins install clawhub:plan-tracker
bash ~/.openclaw/extensions/plan-tracker/scripts/setup.sh
```

#### Option 2: pip

```bash
pip install https://github.com/hinayoung23/plan-tracker/releases/latest/download/plan_tracker-2.10.0-py3-none-any.whl
```

### Setup

```bash
# One command to register MCP server + start daemon
python -m plan_tracker.cli setup
```

`setup` automates:
1. ✅ Registers the MCP server in `~/.openclaw/openclaw.json` (auto-detects Python path)
2. ✅ Starts the daemon (MCP Server watchdog auto-revives it)

> Notification delivery: webhook real-time push is recommended (`webhook-setup`), with queue polling as fallback.

Restart OpenClaw afterwards:
```bash
launchctl unload ~/Library/LaunchAgents/ai.openclaw.gateway.plist
launchctl load ~/Library/LaunchAgents/ai.openclaw.gateway.plist
```

### Notification Delivery

**Option 1: Webhook real-time push (recommended)**

```bash
# Auto-detect channel and install
python -m plan_tracker.cli webhook-setup

# Or specify channel manually
python -m plan_tracker.cli webhook-setup --channel qqbot --to qqbot:c2c:<id>
```

The daemon POSTs notifications to a local webhook receiver, which delivers them instantly via `openclaw agent --deliver`. Notifications are also written to the queue as fallback.

**Option 2: Queue polling**

```bash
python -m plan_tracker.cli deliver
```

Atomically fetches and acks pending notifications.

### Available Tools

| Category | Tool | Description |
|----------|------|-------------|
| Plan | `plan_create` / `plan_get` / `plan_list` / `plan_update` / `plan_delete` | Plan CRUD |
| Plan | `plan_analysis` | Progress statistics and trends |
| Milestone | `milestone_add` / `milestone_update` / `milestone_current` / `milestone_upcoming` | Milestone management |
| Check-in | `checkin_add` | Record a progress check-in |
| Daily | `daily_confirm` / `daily_status` | Daily plan confirmation |
| Reminder | `reminder_configure` / `reminder_toggle` / `reminder_check_now` | Reminder config |
| Notification | `webhook_configure` / `email_configure` | Delivery channel setup |

### Project Structure

```
plan-tracker/
├── plan_tracker/
│   ├── server.py              # FastMCP server (18 tools)
│   ├── daemon.py              # Standalone daemon (double-fork)
│   ├── reminder.py            # Scheduled reminder engine
│   ├── plan_manager.py        # Plan CRUD + analysis
│   ├── milestone_manager.py   # Milestone + check-in logic
│   ├── daily_tracker.py       # Daily state (reminders/timeout/archive)
│   ├── storage.py             # JSON file storage
│   ├── notification_queue.py  # Notification producer-consumer queue
│   ├── cli.py                 # CLI (setup, webhook, deliver, cron)
│   └── notification/          # Delivery channels
│       ├── webhook_channel.py # Webhook push (real-time)
│       ├── email_channel.py   # Email via HMAC-SHA256
│       └── mcp_channel.py     # MCP protocol (queue)
├── scripts/
│   ├── webhook_receiver.py    # HTTP server for webhook delivery
│   └── setup.sh               # One-command install
├── skill/SKILL.md             # AI skill definition
└── data/                      # Runtime data (gitignored)
```

### License

MIT

---

<a id="chinese"></a>
## 中文

### 简介

**Plan Tracker** 是一个 MCP (Model Context Protocol) 服务器，为 AI 助手提供长期计划管理能力。支持里程碑追踪、进度打卡、计划分析和定时提醒。

无论是学习路线、项目规划、健身计划还是读书清单，Plan Tracker 都能帮你把大目标拆解成可执行的里程碑，并持续追踪进度。

### 功能

- **计划管理** — 创建、查看、更新、删除计划，支持分类（学习/项目/健身/阅读/自定义）
- **里程碑管理** — 添加/更新里程碑，查看当前和即将到期的里程碑
- **进度打卡** — 记录每个里程碑的完成百分比、投入时间、心得体会、阻碍和心情
- **计划分析** — 计算进度偏差、节奏系数、剩余工时预估、心情趋势
- **定时提醒** — 基于事件调度，按时触发每日早晚提醒、过期/即将到期/停滞的里程碑检测
- **每日提醒** — 早晚两次提醒：早晨进度提醒（默认 08:30）+ 晚间完成确认（默认 21:30），支持 10 分钟超时自动判定
- **自动拉起** — MCP Server 启动时自动检查并拉起守护进程，附带 watchdog 线程守护存活
- **通知投递** — Webhook 实时推送 + 通知队列兜底，支持多平台（QQ/Telegram/Slack 等），无需轮询

### 环境要求

- Python >= 3.12
- MCP >= 1.0.0

### 安装

#### 方式一：OpenClaw 插件市场（推荐）

```bash
# 一键安装
openclaw plugins install clawhub:plan-tracker

# 初始化配置（Python 依赖 + MCP 注册 + daemon）
bash ~/.openclaw/extensions/plan-tracker/scripts/setup.sh
```

#### 方式二：PyPI / pip

```bash
# 从 GitHub Releases 安装
pip install https://github.com/hinayoung23/plan-tracker/releases/latest/download/plan_tracker-2.10.0-py3-none-any.whl

# 或从源码安装
git clone https://github.com/hinayoung23/plan-tracker.git
cd plan-tracker
pip install -e .
```

### 初始化配置

安装后运行 `setup` 完成 MCP 注册和守护进程启动：

```bash
# 一条命令完成配置
python -m plan_tracker.cli setup

# 试运行：预览改动但不写入
python -m plan_tracker.cli setup --dry-run
```

`setup` 自动完成：
1. ✅ 在 `~/.openclaw/openclaw.json` 中注册 MCP Server（自动检测 Python 路径）
2. ✅ 启动守护进程（MCP Server 内置 watchdog 线程每 5 分钟检测存活，挂了自动拉起）

> 通知投递推荐使用 Webhook 实时推送（`webhook-setup`），也支持队列轮询（`deliver` + cron）。

安装后重启 OpenClaw 生效：
```bash
launchctl unload ~/Library/LaunchAgents/ai.openclaw.gateway.plist
launchctl load ~/Library/LaunchAgents/ai.openclaw.gateway.plist
```

### 可用工具

#### 计划 (Plan)
| 工具 | 说明 |
|------|------|
| `plan_create` | 创建新计划 |
| `plan_get` | 查看计划详情 |
| `plan_list` | 列出所有计划 |
| `plan_update` | 更新计划字段 |
| `plan_delete` | 删除计划 |
| `plan_analysis` | 获取计划分析数据 |

#### 里程碑 (Milestone)
| 工具 | 说明 |
|------|------|
| `milestone_add` | 添加里程碑 |
| `milestone_update` | 更新里程碑 |
| `milestone_current` | 查看当前活跃里程碑 |
| `milestone_upcoming` | 查看即将到期的里程碑 |

#### 打卡 (Check-in)
| 工具 | 说明 |
|------|------|
| `checkin_add` | 记录一次进度打卡 |

#### 每日确认 (Daily)
| 工具 | 说明 |
|------|------|
| `daily_confirm` | 确认当天计划完成情况 |
| `daily_status` | 查看当天提醒和确认状态 |

#### 提醒 (Reminder)
| 工具 | 说明 |
|------|------|
| `reminder_configure` | 配置提醒参数（含每日提醒时间） |
| `reminder_toggle` | 开启/关闭提醒 |
| `reminder_check_now` | 手动触发一次检查 |
| `webhook_configure` | 配置 Webhook 实时推送 |
| `email_configure` | 配置邮件通知（HMAC-SHA256 签名） |

### 数据模型

```
Plan
├── name            (kebab-case, 唯一标识)
├── title           (可读标题)
├── goal            (1-2 句目标描述)
├── category        (learning | project | fitness | reading | custom)
├── target_end_date (YYYY-MM-DD)
├── weekly_hours_target
├── milestones      [Milestone, ...]
└── reminders       ReminderConfig

Milestone
├── id, title, description
├── status          (pending | in_progress | completed | blocked)
├── target_date     (YYYY-MM-DD)
├── completion_pct  (0-100)
├── effort_hours_estimate / effort_hours_actual
└── checkins        [Checkin, ...]

Checkin
├── date, progress_pct, hours_spent
├── notes, blockers
└── morale          (struggling | neutral | good | great)
```

### 部署

Plan Tracker 的提醒功能通过**独立守护进程**运行，支持自动拉起，无需手动管理：

#### MCP Server 自动拉起

当 AI 助手首次调用 plan-tracker MCP 工具时，`server.py` 会自动检查并启动守护进程，同时启动 watchdog 线程每 5 分钟检测守护进程存活状态，挂了自动拉起。

#### 手动管理

```bash
python -m plan_tracker.cli daemon start
python -m plan_tracker.cli daemon status
python -m plan_tracker.cli daemon stop
```

#### 通知投递

**方式一：Webhook 实时推送（推荐）**

```bash
# 一键安装 webhook receiver，自动发现投递渠道
python -m plan_tracker.cli webhook-setup

# 手动指定渠道
python -m plan_tracker.cli webhook-setup --channel qqbot --to qqbot:c2c:<id>
```

Daemon 生成通知后通过 Webhook POST 到本地 receiver，receiver 调用 `openclaw agent --deliver` 实时推送到消息平台，延迟 < 3 秒。通知同时写入队列作为兜底。

**方式二：队列轮询**

```bash
python -m plan_tracker.cli deliver
```

### 提醒机制

#### 每日提醒
- **早晨进度提醒**（默认 08:30）— 提醒当天的计划安排和当前里程碑
- **晚间完成确认**（默认 21:30）— 确认当天计划完成情况（已完成/部分完成/未完成）
- **10 分钟超时** — 晚间确认发出后 10 分钟内未回复，自动标记为未完成
- **补确认归档** — 超时后的补确认将归档到第二天的计划记录中

#### 里程碑提醒
- 基于事件调度，每天在早晨提醒时间触发一次里程碑检查
- 过期里程碑（超过目标日期未完成）→ 推送提醒
- 即将到期（before_due_days 内）→ 推送提醒
- 停滞里程碑（7 天未更新进度）→ 推送提醒
- 每周检查（指定星期几）→ 推送周进度回顾
- 同类型通知 12 小时内不重复推送
- Daemon 启动时自动补检遗漏的提醒

### 项目结构

```
plan-tracker/
├── plan_tracker/
│   ├── server.py              # FastMCP 工具服务入口
│   ├── daemon.py              # 独立守护进程（提醒引擎常驻运行）
│   ├── reminder.py            # 事件调度提醒引擎
│   ├── plan_manager.py        # Plan CRUD + 分析
│   ├── milestone_manager.py   # 里程碑 + 打卡操作
│   ├── daily_tracker.py       # 每日状态管理（提醒/确认/超时/归档）
│   ├── storage.py             # JSON 文件存储
│   ├── notification_queue.py  # 通知队列（daemon 写，外部系统读）
│   ├── cli.py                 # 命令行管理工具
│   └── notification/          # 通知通道
│       ├── webhook_channel.py # Webhook 实时推送
│       ├── email_channel.py   # 邮件通知（HMAC-SHA256）
│       └── mcp_channel.py     # MCP 通道通知
├── scripts/
│   ├── webhook_receiver.py    # Webhook 接收端 HTTP 服务
│   └── setup.sh               # 一键安装脚本
├── skill/SKILL.md             # AI Skill 定义
└── data/                      # 运行时数据（gitignore）
```

### License

MIT
