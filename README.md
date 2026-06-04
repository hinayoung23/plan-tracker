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
- **定时提醒** — 后台线程每 5 分钟轮询，检测过期/即将到期/停滞的里程碑
- **每日提醒** — 早晚两次提醒：早晨进度提醒（默认 08:30）+ 晚间完成确认（默认 21:30），支持 10 分钟超时自动判定
- **多通道通知** — 支持 MCP 通道和邮件通知（邮件可选配置）

### 环境要求

- Python >= 3.12
- MCP >= 1.0.0

### 安装

```bash
# 从 GitHub Releases 安装
pip install https://github.com/hinayoung23/plan-tracker/releases/download/v1.1.0/plan_tracker-1.1.0-py3-none-any.whl

# 或从源码安装
git clone https://github.com/hinayoung23/plan-tracker.git
cd plan-tracker
pip install -e .
```

### 配置到 Claude Code / OpenClaw

在 `openclaw.json` 或 Claude Code 的 MCP 配置中添加：

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

如果是从源码安装且使用虚拟环境，路径示例：

```json
{
  "mcpServers": {
    "plan-tracker": {
      "command": "/path/to/plan-tracker/.venv/bin/python3",
      "args": ["-m", "plan_tracker.server"]
    }
  }
}
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

Plan Tracker 的提醒功能通过**独立守护进程**运行，不依赖任何特定 agent 框架。

```bash
# 如果通过 pip 安装
plan-tracker-cli daemon start

# 或使用模块方式
python -m plan_tracker.cli daemon start

# 查看状态 / 停止
python -m plan_tracker.cli daemon status
python -m plan_tracker.cli daemon stop
```

守护进程将提醒通知写入 `data/notification_queue.json`。外部系统通过以下方式读取通知：

```bash
# CLI 方式（适合 cron / agent 轮询）
python -m plan_tracker.cli notifications

# MCP 方式（适合 AI agent 调用）
# 使用 notification_fetch 和 notification_ack 工具
```

集成到 OpenClaw / QQBot 等 agent 框架时，只需配置一个定时任务（如每 5 分钟）轮询，有输出则转发给用户。

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
- **Milestone Reminders** — Background thread polls every 5 minutes for overdue, upcoming, and stale milestones
- **Multi-channel** — MCP channel and email notifications (email is optional)

### Requirements

- Python >= 3.12
- MCP >= 1.0.0

### Installation

```bash
# From GitHub Releases
pip install https://github.com/hinayoung23/plan-tracker/releases/download/v1.1.0/plan_tracker-1.1.0-py3-none-any.whl

# Or from source
git clone https://github.com/hinayoung23/plan-tracker.git
cd plan-tracker
pip install -e .
```

### Deployment

The reminder engine runs as a standalone daemon, independent of any agent framework:

```bash
# Start / status / stop
plan-tracker-cli daemon start
python -m plan_tracker.cli daemon status
python -m plan_tracker.cli daemon stop
```

The daemon writes notifications to `data/notification_queue.json`. External systems read them via:

```bash
# CLI (for cron / agent polling)
python -m plan_tracker.cli notifications

# MCP (for AI agents)
# Use notification_fetch and notification_ack tools
```

To integrate with OpenClaw / QQBot or similar, set up a periodic task (e.g. every 5 minutes) that polls for notifications and forwards any output to the user.

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
