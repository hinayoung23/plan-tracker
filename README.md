# Plan Tracker MCP

[English](#english) | [中文](#chinese)

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
- **定时提醒** — 守护进程每 5 分钟轮询，检测过期/即将到期/停滞的里程碑
- **每日提醒** — 早晚两次提醒：早晨进度提醒（默认 08:30）+ 晚间完成确认（默认 21:30），支持 10 分钟超时自动判定
- **自动拉起** — MCP Server 启动时自动检查并拉起守护进程，附带 watchdog 线程守护存活
- **多通道通知** — 支持 MCP 通道和邮件通知；通过 OpenClaw cron 定时轮询 + QQBot 推送到 QQ

### 环境要求

- Python >= 3.12
- MCP >= 1.0.0

### 安装

```bash
# 从 GitHub Releases 安装
pip install https://github.com/hinayoung23/plan-tracker/releases/download/v1.3.0/plan_tracker-1.3.0-py3-none-any.whl

# 或从源码安装
git clone https://github.com/hinayoung23/plan-tracker.git
cd plan-tracker
pip install -e .
```

### 初始化配置

安装后运行一条命令完成所有配置（MCP 注册 + 定时通知 + 守护进程）：

```bash
# 完整安装（含 QQ 通知）
plan-tracker-setup setup --qq-id "你的QQ十六进制ID"

# 或使用模块方式
python -m plan_tracker.cli setup --qq-id "你的QQ号"

# 试运行：预览所有改动但不写入
python -m plan_tracker.cli setup --qq-id "你的QQ号" --dry-run
```

> **QQ ID 获取方式**：在 QQ Bot 与你的私聊消息中，OpenClaw 日志会显示目标十六进制 ID。

`setup` 命令自动完成：
1. ✅ 在 `~/.openclaw/openclaw.json` 中注册 MCP Server（自动检测 Python 路径）
2. ✅ 安装 OpenClaw cron 定时任务（自动生成正确时间戳，无需手写）
3. ✅ 启动守护进程

安装后重启 OpenClaw 生效：
```bash
launchctl unload ~/Library/LaunchAgents/ai.openclaw.gateway.plist
launchctl load ~/Library/LaunchAgents/ai.openclaw.gateway.plist
```

如果需要手动配置（不使用 setup），参考下面的部署章节。

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
| `email_configure` | 配置邮件通知 |

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

### 部署（首次使用）

Plan Tracker 的提醒功能通过**独立守护进程**运行。守护进程支持两种自动拉起方式，无需手动管理：

#### 方式一：MCP Server 自动拉起（推荐）

当 AI 助手首次调用 plan-tracker MCP 工具时，`server.py` 会自动检查并启动守护进程，同时启动 watchdog 线程每 5 分钟检测守护进程存活状态，挂了自动拉起。

```bash
# 无需手动操作 —— MCP Server 启动时自动完成
```

#### 方式二：CLI 轮询自愈

`notifications --ack` 命令在执行前会自动检查守护进程是否在运行，未运行则自动拉起。配合定时任务即可实现全自动运维。

```bash
# 查看通知（自动拉起守护进程 + 标记已投递）
python -m plan_tracker.cli notifications --ack

# 单独管理守护进程（一般不需要）
python -m plan_tracker.cli daemon start
python -m plan_tracker.cli daemon status
python -m plan_tracker.cli daemon stop
```

#### 集成到 OpenClaw / QQBot

使用 `cron-setup` 命令一键安装 OpenClaw 定时轮询任务，自动生成正确的时间戳并写入配置：

```bash
# 一键安装（替换为你的 QQ ID）
python -m plan_tracker.cli cron-setup --qq-id "你的QQ号十六进制"

# 试运行：只打印 JSON 不写入
python -m plan_tracker.cli cron-setup --qq-id "你的QQ号" --dry-run

# 自定义轮询间隔（分钟）
python -m plan_tracker.cli cron-setup --qq-id "你的QQ号" --interval 10
```

> **QQ ID 获取方式**：在 QQ Bot 与你的私聊消息中，OpenClaw 日志会显示发送目标的十六进制 ID（如 `82B0F3FE4CA79ED6FAEE3A6BC65F25AB`）。

安装后重启 OpenClaw 生效：
```bash
launchctl unload ~/Library/LaunchAgents/ai.openclaw.gateway.plist
launchctl load ~/Library/LaunchAgents/ai.openclaw.gateway.plist
```

守护进程将提醒通知写入 `data/notification_queue.json`，cron 任务定时拉取并投递。`--ack` 参数确保每条通知只投递一次。

### 提醒机制

#### 每日提醒
- **早晨进度提醒**（默认 08:30）— 提醒当天的计划安排和当前里程碑
- **晚间完成确认**（默认 21:30）— 确认当天计划完成情况（已完成/部分完成/未完成）
- **10 分钟超时** — 晚间确认发出后 10 分钟内未回复，自动标记为未完成
- **补确认归档** — 超时后的补确认将归档到第二天的计划记录中
- 两种提醒的触发时间均可在 `reminder_configure` 中自定义

#### 里程碑提醒
- 后台线程每 5 分钟检查一次所有计划
- 过期里程碑（超过目标日期未完成）→ 推送提醒
- 即将到期（before_due_days 内）→ 推送提醒
- 停滞里程碑（7 天未更新进度）→ 推送提醒
- 每周检查（指定星期几）→ 推送周进度回顾
- 同类型通知 12 小时内不重复推送

### 项目结构

```
plan-tracker/
├── daemon.py              # 独立守护进程（提醒引擎常驻运行）
├── server.py              # FastMCP 工具服务入口
├── cli.py                 # 命令行管理工具
├── notification_queue.py  # 通知队列（daemon 写，外部系统读）
├── plan_manager.py        # Plan CRUD + 分析
├── milestone_manager.py   # 里程碑 + 打卡操作
├── daily_tracker.py       # 每日状态管理（提醒/确认/超时/归档）
├── storage.py             # JSON 文件存储
├── reminder.py            # 提醒引擎逻辑
├── notification/
│   ├── __init__.py
│   ├── base.py            # 通知通道基类
│   ├── mcp_channel.py     # MCP 通道通知
│   └── email_channel.py   # 邮件通知
├── skill/
│   └── SKILL.md           # AI Skill 定义
├── data/                  # 计划数据（gitignore）
└── pyproject.toml
```

### License

MIT

---

<a id="english"></a>
## English

### Overview

**Plan Tracker** is an MCP (Model Context Protocol) server that gives AI assistants long-term plan management capabilities. It supports milestone tracking, progress check-ins, plan analysis, and scheduled reminders.

Whether it's a learning roadmap, project plan, fitness goal, or reading list, Plan Tracker helps break big goals into executable milestones and continuously tracks progress.

### Features

- **Plan CRUD** — Create, view, update, delete plans with categories (learning/project/fitness/reading/custom)
- **Milestones** — Add and update milestones, view current and upcoming ones
- **Check-ins** — Record progress percentage, time spent, notes, blockers, and morale for each milestone
- **Analysis** — Progress deviation, pace ratio, remaining effort estimates, morale trends
- **Daily Reminders** — Morning check-in (default 08:30) + evening review (default 21:30) with 10-min auto-timeout
- **Milestone Reminders** — Daemon polls every 5 minutes for overdue, upcoming, and stale milestones
- **Auto-start Daemon** — MCP Server auto-starts the daemon on first use with a watchdog thread; CLI `--ack` also self-heals
- **Multi-channel** — MCP channel + email; OpenClaw cron + QQBot integration for push notifications

### Requirements

- Python >= 3.12
- MCP >= 1.0.0

### Installation

```bash
# From GitHub Releases
pip install https://github.com/hinayoung23/plan-tracker/releases/download/v1.3.0/plan_tracker-1.3.0-py3-none-any.whl

# Or from source
git clone https://github.com/hinayoung23/plan-tracker.git
cd plan-tracker
pip install -e .
```

### Setup

One command handles everything — MCP registration, cron job, and daemon:

```bash
# Full setup with QQ notifications
plan-tracker-setup setup --qq-id "your-qq-hex-id"

# Or using the module
python -m plan_tracker.cli setup --qq-id "your-qq-id"

# Preview changes without writing
python -m plan_tracker.cli setup --qq-id "your-qq-id" --dry-run
```

> **Finding your QQ ID**: in a private chat with your QQ Bot, OpenClaw logs will show the target hex ID.

The `setup` command automates:
1. ✅ Registers the MCP server in `~/.openclaw/openclaw.json` (auto-detects Python path)
2. ✅ Installs the OpenClaw cron job (auto-generates timestamps — no manual editing)
3. ✅ Starts the daemon

Restart OpenClaw afterwards:
```bash
launchctl unload ~/Library/LaunchAgents/ai.openclaw.gateway.plist
launchctl load ~/Library/LaunchAgents/ai.openclaw.gateway.plist
```

For manual configuration (without `setup`), see the [manual deployment](#manual-deployment) section below.

#### OpenClaw / QQBot integration

Use the `cron-setup` command to install the cron job with a single command — timestamps are always correct because they're generated automatically:

```bash
# One-shot install (replace with your QQ ID)
python -m plan_tracker.cli cron-setup --qq-id "your-qq-hex-id"

# Dry-run: print the JSON without writing
python -m plan_tracker.cli cron-setup --qq-id "your-qq-id" --dry-run

# Custom polling interval (minutes)
python -m plan_tracker.cli cron-setup --qq-id "your-qq-id" --interval 10
```

> **Finding your QQ ID**: in a private chat with your QQ Bot, OpenClaw logs will show the target hex ID (e.g. `82B0F3FE4CA79ED6FAEE3A6BC65F25AB`).

Restart OpenClaw afterwards:
```bash
launchctl unload ~/Library/LaunchAgents/ai.openclaw.gateway.plist
launchctl load ~/Library/LaunchAgents/ai.openclaw.gateway.plist
```

The daemon writes notifications to `data/notification_queue.json`. The cron job polls and delivers them. The `--ack` flag ensures each notification is delivered exactly once.

### MCP Configuration

Add to your `openclaw.json` or Claude Code MCP config:

```json
{
  "mcpServers": {
    "plan-tracker": {
      "command": "python3",
      "args": ["-m", "plan_tracker.server"]
    }
  }
}
```

### License

MIT
