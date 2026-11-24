# Plan Tracker

[简体中文](README.md) · **English**

Plan Tracker is an MCP server and OpenClaw plugin for managing long-term plans. In chat, you can break a learning, project, or fitness goal into milestones, log time and progress, and receive reminders through your messaging channel.

[Installation](#install) · [Quick start](#quick-start) · [Notifications and reminders](#notifications) · [Architecture](#architecture) · [Tools and development](#reference) · [Releases](https://github.com/hinayoung23/plan-tracker/releases)

<a id="install"></a>

## Install the OpenClaw plugin

Complete OpenClaw's initial setup, then choose one of the following two ways to install the plugin. [OpenClaw getting started guide](https://docs.openclaw.ai/start/getting-started)

### Download from GitHub

```bash
release_url=https://github.com/hinayoung23/plan-tracker/releases/download/v2.14.2
curl -fLO "$release_url/plan-tracker-2.14.2.tgz"
openclaw plugins install ./plan-tracker-2.14.2.tgz
```

### Install from ClawHub

```bash
openclaw plugins install clawhub:plan-tracker
```

## Quick start

### Set up the Python service

The plugin supplies the skill and the notification delivery command. Both plugin installation methods use the same separate Python service for planning tools, data storage, and reminder scheduling. You need **Python 3.12 or later**. The commands below work in Bash and Zsh.

Download the matching service source archive from GitHub Releases and extract it where you plan to keep it. The setup script prepares the dependencies, registers the MCP server, and starts the reminder daemon. This example uses v2.14.2.

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

For another MCP client, register the virtual environment's Python executable as a stdio server with the arguments `["-m", "plan_tracker.server"]`.

### Use Plan Tracker in chat

Once setup is complete, try these requests in OpenClaw.

- “Create an eight-week Rust learning plan with four milestones and a budget of five hours a week.”
- “Log a check-in. I spent two hours studying today and finished 40% of the first milestone.”
- “Show my progress and the next milestone due. Am I behind schedule?”

You can also list your plans with `python -m plan_tracker.cli plan list`. To receive reminders in chat, set up [webhook delivery](#notifications).

<a id="notifications"></a>

## Notifications and reminders

### Webhook delivery to OpenClaw

On macOS, `webhook-setup` installs and starts the local receiver. Configure a messaging channel in OpenClaw, then create a local `delivery.json` file. This example targets a QQ direct message. Replace `<id>` with your recipient ID. The `agentId` field defaults to `main` and can be set to another agent.

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

The setup command saves the delivery configuration as `webhook_delivery.json` in the data directory. Subsequent runs can reuse that file. The receiver listens at `http://127.0.0.1:9876` by default. If you change the port with `--port`, use `webhook_configure` to update the webhook URL for each affected plan.

### Queue and email delivery

Custom delivery programs can consume the queue with the MCP tools `notification_fetch` and `notification_ack`. With the CLI, fetch pending items first and acknowledge each ID after successful delivery.

```bash
python -m plan_tracker.cli notification fetch
python -m plan_tracker.cli notification ack NOTIFICATION_ID
```

Use `deliver --no-ack` to preview messages as text. By default, `deliver` acknowledges queue entries after printing them. For older OpenClaw deployments that still read `~/.openclaw/cron/jobs.json`, see `cron-setup --help` for polling options. For email, enable the `email` channel and use `email_configure` to set the mail API credentials.

### Reminder schedule

| Reminder | Default behavior |
| --- | --- |
| Morning check-in | At 08:30 local time, shows the current milestone |
| Evening review | At 21:30 local time, allows 10 minutes from when the daemon records the reminder |
| Milestone reminders | Checks for overdue, upcoming, and stalled milestones. Upcoming alerts start 3 days before the due date |
| Weekly review | Summarizes progress at 09:00 local time on Mondays |

An evening review can be confirmed as completed, partial, or incomplete. Logging milestone progress in chat during the confirmation window also confirms that day's review. An unconfirmed review is marked incomplete when the window expires. A late manual confirmation is archived with the next day's record.

On startup, the daemon checks the preceding seven days for evening reviews with an expired confirmation window and no confirmation. Since v2.14.2, timeout notifications show the affected dates, and catch-up groups dates for the same plan into one notification.

To change these times, replace `rust-learning` with your plan ID in this example.

```bash
python -m plan_tracker.cli reminder configure --plan rust-learning \
  --daily-checkin-time 08:30 --daily-review-time 21:30 \
  --confirmation-timeout-minutes 10
```

## Architecture

| Component | Responsibility |
| --- | --- |
| MCP server | Exposes tools for plans, milestones, check-ins, analysis, and reminder configuration |
| Reminder daemon | Schedules reminders independently, records daily review state, and enqueues notifications |
| Webhook receiver | Reads pending notifications, sends them through the Gateway with `openclaw plan-tracker-deliver`, and acknowledges each successful delivery |
| Local data | Stores plans, check-ins, daily state, the notification queue, and logs |

On macOS, the `com.plan-tracker.daemon` LaunchAgent manages the daemon. On other platforms, you can start the daemon manually, and the MCP server also checks for it and attempts to start it when needed. See `python -m plan_tracker.cli daemon --help` for start, stop, and status commands.

The setup above defaults to the `data/` directory in the extracted source. Set `PLAN_TRACKER_DATA_DIR` to override this path and use the same directory for the MCP server, daemon, receiver, and CLI. For troubleshooting, check `daemon.log` for reminder generation and `webhook-stderr.log` for delivery.

To upgrade the Python service, point `PLAN_TRACKER_DATA_DIR` at your existing data directory before running the new version's `scripts/setup.sh`. If you use webhook delivery, rerun `webhook-setup` from the new Python environment to reuse the delivery configuration in that directory.

<a id="reference"></a>

## Tools and development

| Task | MCP tools |
| --- | --- |
| Manage plans and review progress | `plan_create`, `plan_get`, `plan_list`, `plan_update`, `plan_delete`, `plan_analysis` |
| Manage milestones and log check-ins | `milestone_add`, `milestone_update`, `milestone_current`, `milestone_upcoming`, `checkin_add` |
| View or confirm today's progress | `daily_status`, `daily_confirm` |
| Configure reminders and delivery channels | `reminder_configure`, `reminder_toggle`, `reminder_check_now`, `webhook_configure`, `email_configure` |
| Consume the notification queue | `notification_fetch`, `notification_ack` |

A plan has a goal, deadline, and weekly time target. Milestones track status, progress, and estimated hours. Check-ins record progress, time spent, notes, blockers, and morale. For the full data model and conversation workflows, see the [Plan Tracker skill](skill/SKILL.md).

### Build locally

```bash
git clone https://github.com/hinayoung23/plan-tracker.git
cd plan-tracker
python3 scripts/build_openclaw_runtime.py
```

The build command writes the JavaScript entry point, skill, manifests, documentation, and license to `dist/openclaw-runtime/<content-hash>/`.

On macOS, run `./check.sh` for the pre-release checks after setting up the Python service environment and installing Node.js. Report bugs and suggestions in [GitHub Issues](https://github.com/hinayoung23/plan-tracker/issues).

## License

[MIT](LICENSE)
