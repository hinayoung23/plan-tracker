# Plan Tracker

<a id="chinese"></a>
<a id="english"></a>

**简体中文** · [English](https://github.com/hinayoung23/plan-tracker/blob/main/README.en.md)

Plan Tracker 是一个长期计划管理 MCP 服务，也可作为 OpenClaw 插件使用。你可以在对话中创建学习、项目或健身计划，将目标拆成里程碑，记录投入时间和进度，并在常用聊天渠道接收提醒。

[安装](#install) · [快速上手](#quick-start) · [通知与提醒](#notifications) · [运行架构](#architecture) · [工具与开发](#reference) · [版本发布](https://github.com/hinayoung23/plan-tracker/releases)

<a id="install"></a>

## 安装 OpenClaw 插件

先完成 OpenClaw 的初始化，再任选以下一种方式安装插件。[OpenClaw 入门](https://docs.openclaw.ai/start/getting-started)

### 从 GitHub 下载

```bash
release_url=https://github.com/hinayoung23/plan-tracker/releases/download/v2.14.2
curl -fLO "$release_url/plan-tracker-2.14.2.tgz"
openclaw plugins install ./plan-tracker-2.14.2.tgz
```

### 从 ClawHub 安装

```bash
openclaw plugins install clawhub:plan-tracker
```

<a id="quick-start"></a>

## 快速上手

### 初始化 Python 服务

插件提供技能和通知投递命令。两种插件安装方式共用独立的 Python 服务，由它提供计划工具、数据存储和提醒调度。需要 **Python 3.12 或更新版本**，以下命令适用于 Bash 或 Zsh。

从 GitHub Releases 下载对应版本的服务源码包，解压到长期保留的位置。配置脚本会准备依赖、注册 MCP 服务并启动提醒 daemon。下面以 v2.14.2 为例。

```bash
release_url=https://github.com/hinayoung23/plan-tracker/releases/download/v2.14.2
curl -fLO "$release_url/plan_tracker-2.14.2.tar.gz"
tar -xzf plan_tracker-2.14.2.tar.gz
cd plan_tracker-2.14.2
python3 -m venv .venv
bash scripts/setup.sh
source .venv/bin/activate

openclaw gateway restart
python -m plan_tracker.cli daemon status
```

其他 MCP 客户端可将该虚拟环境中的 Python 设为 stdio 命令，参数数组为 `["-m", "plan_tracker.server"]`。

### 在对话中使用

注册完成后，可以在 OpenClaw 对话中提出这些请求。

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

按上述步骤配置时，默认使用解压目录中的 `data/`。可用 `PLAN_TRACKER_DATA_DIR` 指定其他目录，MCP 服务、daemon、接收器和 CLI 应使用同一路径。排查提醒生成和实际投递时，分别查看该目录的 `daemon.log` 和 `webhook-stderr.log`。

升级 Python 服务时，在运行新版的 `scripts/setup.sh` 前，将 `PLAN_TRACKER_DATA_DIR` 指向现有数据目录。若同时使用 Webhook 推送，用新 Python 环境重新运行 `webhook-setup`，可复用原目录中的接收配置。

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

### 本地构建

```bash
git clone https://github.com/hinayoung23/plan-tracker.git
cd plan-tracker
python3 scripts/build_openclaw_runtime.py
```

构建命令会将 JS 入口、技能、清单、文档和许可证输出到 `dist/openclaw-runtime/<内容哈希>/`。

在 macOS 上，准备好 Python 服务环境并安装 Node.js 后，可运行 `./check.sh` 完成发布前检查。问题和建议可提交到 [GitHub Issues](https://github.com/hinayoung23/plan-tracker/issues)。

## 许可证

[MIT](LICENSE)
