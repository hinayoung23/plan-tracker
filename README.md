# Plan Tracker

<a id="chinese"></a>

Plan Tracker 是一个长期计划管理 MCP 服务，也可作为 OpenClaw 插件使用。你可以在对话中创建学习、项目或健身计划，将目标拆成里程碑，记录投入时间和进度，并在常用聊天渠道接收提醒。

[安装](#install) · [快速上手](#quick-start) · [通知与提醒](#notifications) · [运行架构](#architecture) · [工具与开发](#reference) · [English](#english) · [版本发布](https://github.com/hinayoung23/plan-tracker/releases)

<a id="install"></a>

## 安装

需要 **Python 3.12 或更新版本**。安装 Python 服务时会一并安装 MCP 依赖。使用 OpenClaw 时，请先完成 OpenClaw 的初始化。以下命令适用于 Bash 或 Zsh。[OpenClaw 入门](https://docs.openclaw.ai/start/getting-started)

下面以 v2.14.2 为例，从同一个 GitHub Release 安装 OpenClaw 插件和 Python 服务。

```bash
release_url=https://github.com/hinayoung23/plan-tracker/releases/download/v2.14.2
curl -fLO "$release_url/plan-tracker-2.14.2.tgz"
openclaw plugins install ./plan-tracker-2.14.2.tgz

python3 -m venv "$HOME/.local/share/plan-tracker/venv"
source "$HOME/.local/share/plan-tracker/venv/bin/activate"
python -m pip install "$release_url/plan_tracker-2.14.2-py3-none-any.whl"
```

OpenClaw 插件提供技能和通知投递命令。计划工具、数据存储和提醒调度由 Python 服务提供。也可以从 ClawHub 安装其已发布的插件版本，命令是 `openclaw plugins install clawhub:plan-tracker`。[OpenClaw 插件安装指南](https://docs.openclaw.ai/cli/plugins/install)

其他 MCP 客户端可以直接安装 Python wheel，将虚拟环境中的 Python 设为 stdio 命令，参数数组为 `["-m", "plan_tracker.server"]`。

<a id="quick-start"></a>

## 快速上手

在上一步激活的 Python 环境中运行以下命令。`setup` 将 MCP 服务注册到 `~/.openclaw/openclaw.json`，并启动提醒 daemon。

```bash
python -m plan_tracker.cli setup
openclaw gateway restart
python -m plan_tracker.cli daemon status
```

可以先用 `python -m plan_tracker.cli setup --dry-run` 预览配置。注册完成后，可以在 OpenClaw 对话中提出这些请求。

- 「帮我创建一个 8 周的 Rust 学习计划，每周投入 5 小时，拆成 4 个里程碑。」
- 「打卡。第一个里程碑完成了 40%，今天学了 2 小时。」
- 「查看计划进展和下一个到期的里程碑，告诉我现在是否落后。」

也可以用 `python -m plan_tracker.cli plan list` 查看计划列表。需要将提醒送到聊天渠道时，继续配置 [Webhook 推送](#notifications)。

<a id="notifications"></a>

## 通知与提醒

### Webhook 推送到 OpenClaw

macOS 可以用 `webhook-setup` 安装并启动本地接收器。先确保 OpenClaw 已配置可用的聊天渠道，再在本机创建 `delivery.json`。以下是 QQ 私聊示例，将 `<id>` 替换为你的接收目标。`agentId` 默认是 `main`，可按需指定其他 Agent。

```json
{
  "channel": "qqbot",
  "to": "qqbot:c2c:<id>",
  "agentId": "main"
}
```

```bash
chmod 600 delivery.json
python -m plan_tracker.cli webhook-setup --delivery-config ./delivery.json
openclaw plan-tracker-deliver --help
```

接收配置保存在数据目录的 `webhook_delivery.json` 中。再次运行 `webhook-setup` 时可以复用已保存的配置。默认接收地址是 `http://127.0.0.1:9876`，用 `--port` 改端口时，也要用 `webhook_configure` 更新计划中的地址。

### 队列与邮件

自定义投递程序可以通过 MCP 的 `notification_fetch` 和 `notification_ack` 消费队列。使用 CLI 时先拉取通知，实际发送成功后再用对应的 ID 确认。

```bash
python -m plan_tracker.cli notification fetch
python -m plan_tracker.cli notification ack NOTIFICATION_ID
```

`deliver --no-ack` 可预览文本，`deliver` 默认会在打印后确认队列记录。仍使用 `~/.openclaw/cron/jobs.json` 的旧版 OpenClaw 部署可通过 `cron-setup --help` 查看轮询配置。邮件投递由 `email_configure` 配置，需要启用 `email` 通道和对应的邮件 API 凭据。

### 提醒规则

| 提醒 | 默认行为 |
| --- | --- |
| 早晨进度提醒 | 本地时间 08:30，提示当前里程碑 |
| 晚间完成确认 | 本地时间 21:30，从 daemon 记录提醒时起默认有 10 分钟确认时间 |
| 里程碑提醒 | 检查逾期、临近到期和进度停滞的里程碑，默认提前 3 天提示 |
| 每周回顾 | 默认周一 09:00 汇总计划进度 |

晚间确认支持「完成」「部分完成」「未完成」。在确认窗口内通过对话记录里程碑进度，也会自动确认当天的晚间提醒。超时未确认会自动标记为未完成，超时后的手动补确认会归档到次日记录。

daemon 启动时会补检最近 7 天内已发出晚间提醒、已超时且尚未确认的记录。自 v2.14.2 起，超时通知注明对应日期，同一计划的多条补检结果会汇成一条。

配置其他时间时，将下例中的 `rust-learning` 换成你的计划标识。

```bash
python -m plan_tracker.cli reminder configure --plan rust-learning \
  --daily-checkin-time 08:30 --daily-review-time 21:30 \
  --confirmation-timeout-minutes 10
```

<a id="architecture"></a>

## 运行架构

| 组件 | 职责 |
| --- | --- |
| MCP 服务 | 提供计划、里程碑、打卡、分析和提醒配置工具 |
| 提醒 daemon | 独立调度提醒，记录每日确认状态并写入通知队列 |
| Webhook 接收器 | 读取待投递通知，调用 `openclaw plan-tracker-deliver` 经 Gateway 发送，成功后逐条确认 |
| 本地数据 | 保存计划、打卡、每日状态、通知队列和日志 |

在 macOS 上，daemon 由 `com.plan-tracker.daemon` LaunchAgent 托管。其他平台可以手动启动 daemon，MCP 服务运行时也会检查并尝试拉起它。使用 `python -m plan_tracker.cli daemon --help` 查看启动、停止和状态命令。

源码部署默认使用项目的 `data/` 目录，wheel 安装默认使用 `~/mcp-servers/plan-tracker/data/`。可用 `PLAN_TRACKER_DATA_DIR` 指定其他目录，MCP 服务、daemon、接收器和 CLI 应使用同一路径。排查提醒生成和实际投递时，分别查看该目录的 `daemon.log` 和 `webhook-stderr.log`。

升级已有部署时，请在当前 Python 环境安装新 wheel，保留原数据目录。重启提醒 daemon 后，调度修复才会生效。macOS 可在原环境重新运行 `python -m plan_tracker.cli setup` 来加载新版 daemon。

<a id="reference"></a>

## 工具与开发

| 用途 | MCP 工具 |
| --- | --- |
| 管理计划和查看进度 | `plan_create`、`plan_get`、`plan_list`、`plan_update`、`plan_delete`、`plan_analysis` |
| 安排里程碑和打卡 | `milestone_add`、`milestone_update`、`milestone_current`、`milestone_upcoming`、`checkin_add` |
| 查看或确认当天完成情况 | `daily_status`、`daily_confirm` |
| 设置提醒与通知渠道 | `reminder_configure`、`reminder_toggle`、`reminder_check_now`、`webhook_configure`、`email_configure` |
| 消费通知队列 | `notification_fetch`、`notification_ack` |

计划包含目标、截止日期和周投入目标。里程碑记录状态、进度和预估工时，打卡记录进度、投入、笔记、阻碍和心情。完整字段和对话流程见 [Plan Tracker 技能文档](skill/SKILL.md)。

### 从源码运行

```bash
git clone https://github.com/hinayoung23/plan-tracker.git
cd plan-tracker
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python scripts/build_openclaw_runtime.py
```

构建命令会输出 `dist/openclaw-runtime/<内容哈希>/`。在 OpenClaw 的 `plugins.load.paths` 中添加该目录，保留已有的其他插件路径。若之前直接加载项目根目录，用构建目录替换对应路径。轻量目录包含 JS 入口、技能、清单、文档和许可证。Python 环境和数据保存在独立路径。

在 macOS 安装 Node.js 后，可运行 `./check.sh` 完成发布前检查。问题和建议可提交到 [GitHub Issues](https://github.com/hinayoung23/plan-tracker/issues)。

<a id="english"></a>

## English quick start

Plan Tracker is an MCP server and an OpenClaw plugin for long-term plans, milestones, progress check-ins, and scheduled reminders. Install the plugin archive and Python wheel from the same [GitHub Release](https://github.com/hinayoung23/plan-tracker/releases) with Python 3.12+.

1. Follow the [installation commands](#install), then run `python -m plan_tracker.cli setup` and `openclaw gateway restart`.
2. Ask OpenClaw to create a plan with milestones, log progress, or review upcoming deadlines.
3. For chat notifications on macOS, follow the [Webhook setup](#notifications). Store the channel and recipient in a private JSON file and pass it via `--delivery-config`.

See the [skill documentation](skill/SKILL.md) for tools and workflows, and the [release notes](https://github.com/hinayoung23/plan-tracker/releases) for changes.

## 许可证

[MIT](LICENSE)
