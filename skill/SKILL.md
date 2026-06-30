---
name: plan-tracker
description: Long-term plan tracking with milestone check-ins, progress analysis, and scheduled reminders. Use when the user wants to create, manage, check in on, or analyze a long-term plan (learning, project, fitness, reading, custom).
metadata:
  openclaw:
    emoji: "📋"
    requires:
      bins: ["python3"]
      mcp:
        name: "plan-tracker"
        command: "~/mcp-servers/plan-tracker/.venv/bin/python3"
        args: ["-m", "plan_tracker.server"]
---

# Plan Tracker

Track long-term plans with structured milestones, check-ins, and proactive reminders.

## Architecture

This skill works with the plan-tracker MCP Server. The MCP server handles data storage and reminders. The AI (you) handles analysis, suggestions, and natural language interaction.

**MCP Server tools available:**
- `plan_create` / `plan_get` / `plan_list` / `plan_update` / `plan_delete` / `plan_analysis`
- `milestone_add` / `milestone_update` / `milestone_current` / `milestone_upcoming`
- `checkin_add`
- `daily_confirm` / `daily_status` — daily plan completion confirmation
- `reminder_configure` / `reminder_toggle` / `email_configure` / `reminder_check_now`
- Email sending via REST API (mail.tempbox.cn, free for plan-tracker)

---

## Data Model

```
Plan
├── name (kebab-case, unique identifier)
├── title (human-readable)
├── goal (1-2 sentence objective)
├── description (optional detail)
├── category: learning | project | fitness | reading | custom
├── tags: [string, ...]
├── created_at / updated_at (ISO 8601)
├── target_end_date (YYYY-MM-DD)
├── weekly_hours_target (integer)
├── milestones: [Milestone, ...]
└── reminders: ReminderConfig

Milestone
├── id: "ms-001"
├── title
├── description
├── order (1-based)
├── status: pending | in_progress | completed | blocked
├── target_date (YYYY-MM-DD)
├── actual_date (YYYY-MM-DD or null)
├── completion_pct (0-100)
├── effort_hours_estimate (integer)
├── effort_hours_actual (integer or null)
├── notes
└── checkins: [Checkin, ...]

Checkin
├── date (ISO 8601)
├── progress_pct (0-100)
├── hours_spent
├── notes
├── blockers
└── morale: struggling | neutral | good | great

ReminderConfig
├── enabled: bool
├── before_due_days: int (default 3)
├── weekly_checkin_day: monday|...|sunday|""
├── weekly_checkin_time: "HH:MM"
├── notification_channels: ["mcp"] | ["mcp", "email"]
└── email: { enabled, api_url, api_key, recipient }
```

---

## Milestone Lifecycle

```
pending  ──first checkin (progress > 0%)──→  in_progress
                                              │
                          ┌────────────────────┤
                          │                    │
                     progress=100%        blocker reported
                          │                    │
                          v                    v
                      completed            blocked
                                               │
                                          blocker resolved
                                               │
                                               v
                                          in_progress
```

MCP `checkin_add` handles status transitions automatically:
- progress_pct >= 100 → status = "completed", actual_date = today
- progress_pct > 0 AND status = "pending" → status = "in_progress"
- blockers text is non-empty → appended to milestone notes

---

## Conversational Workflows

### Workflow 1: Create a Plan

```
User: "帮我创建一个 Python ML 学习计划"

1. Gather requirements via AskUserQuestion:
   - Plan title
   - Goal description
   - Target end date
   - Weekly hours available
   - Category
   - Milestones: user provides list OR you generate suggestions

2. If generating milestones:
   - Apply SMART principles
   - Each milestone 1-4 weeks of work
   - Ensure prerequisite ordering
   - Suggest titles, descriptions, target dates, hour estimates

3. Present draft and ask for confirmation

4. Call plan_create with confirmed data

5. Ask: "是否设置定时提醒？"
   If yes, call reminder_configure with defaults (before_due_days=3, weekly_checkin_day="monday", weekly_checkin_time="09:00")
```

### Workflow 2: Check In

```
User: "/plan-tracker:check" or "check in Rust 计划"

1. Call milestone_current to find the active milestone

2. Ask structured questions:
   - "完成了百分之多少？" (0-100)
   - "花了多少小时？" (integer)
   - "学到了什么 / 做了什么？" (notes)
   - "有没有遇到什么阻碍？" (blockers)
   - "感觉如何？" (struggling / neutral / good / great)

3. Call checkin_add with user's answers

4. Read the returned milestone to see if status changed

5. Compute pace from plan_analysis:
   - If pace > 1.2 (20% slower than estimated): show warning and offer update
   - If pace < 0.8 (20% faster): encourage
   - If blocked: ask if they want suggestions to overcome it
   - If completed: congratulate and ask if ready to start next
```

### Workflow 3: Show Status

```
User: "看下我的计划进展"

1. Call plan_list to get all plans
2. Let user pick one (or auto-pick if only one)
3. Call plan_get + plan_analysis
4. Render:

## Plan: {title}
**Goal**: {goal}
**Overall**: [{====>----}] {progress_pct}% | Target: {target_end_date} | Days left: {days_remaining}
**Status**: {status}

### Milestones
MS-001 {status_icon} {title}
  Target: {target_date} | Est: {est}h | Actual: {actual}h | {completion_pct}%
  {status_description}

### Key Metrics
- Pace: {average_pace}x (1.0 = on track)
- Remaining effort: {adjusted_remaining_hours}h (estimated)
- Time elapsed: {time_elapsed_pct}%
```

### Workflow 4: Adjust Plan

```
User: "调整一下 Rust 学习计划"

1. Call plan_analysis to get metrics

2. Calculate:
   - avg_pace = average_pace from analysis
   - remaining_est = remaining_est_hours
   - adjusted = adjusted_remaining_hours
   - days_remaining = days_remaining
   - weekly = weekly_hours_target
   - hours_available = days_remaining * weekly / 7

3. Present options:
   Option A: Extend deadline
     new_days = remaining_est * avg_pace / weekly * 7
     new_end_date = today + new_days
   Option B: Reduce scope (drop or merge milestones)
   Option C: Increase weekly hours
     new_weekly = adjusted / (days_remaining / 7)

4. Ask user to choose and confirm

5. Call plan_update to apply changes
```

### Workflow 5: Get Suggestions

```
User: "帮我看看计划是否合理"

1. Call plan_get to load full plan

2. Evaluate each milestone:
   - Specificity: Is it clear what "done" means?
   - Measurability: Can progress be verified?
   - Size: Each milestone should be 1-4 weeks
   - Ordering: Does ms[i+1] depend on ms[i]?
   - Gaps: Are there missing prerequisites?

3. Present categorized suggestions:
   - Milestone quality (rewording needed, splitting needed)
   - Time realism (estimates are off)
   - Resource recommendations
   - Ordering issues

4. Ask: "要应用这些建议吗？"
```

### Workflow 6: Track an X/Twitter Launch

```
User: "Track our launch monitoring plan with TweetClaw"

1. Gather the launch goal, X/Twitter queries or accounts, approval owner,
   reporting cadence, and target end date.

2. Create a project plan with milestones:
   - Define source scope and success metrics
   - Configure approved TweetClaw monitors or exports in OpenClaw
   - Review daily signal counts, blockers, and next actions
   - Produce the final report and close follow-up posting tasks

3. For each check-in, record:
   - Monitor or export status
   - New risks or blockers
   - Approval state for any posting, reply, DM, webhook, or follow-up action

4. If the user asks to post, reply, send DMs, change monitors, or alter webhooks,
   confirm the exact approved action before marking that milestone complete.
```

---

## Adjustment Heuristics

### Pace Calculation
```
For completed milestones with both est and actual hours:
  pace = actual / est
average_pace = mean(all paces)
If no completed milestones: extrapolate from in_progress (current_pct / hours_spent)
```

### When to Suggest Adjustment
- average_pace > 1.2: slower than expected, plan may need extension
- average_pace < 0.8: faster than expected, could add enrichment
- time_elapsed_pct > progress_pct + 15: significantly behind schedule
- 2+ consecutive "struggling" morale entries: possible burnout, suggest scope reduction
- Same blocker in 2+ checkins: structural issue, suggest milestone restructuring

### Adjustment Strategies
- **Extend**: new_end = today + (remaining_days / max(1, 1/avg_pace))
- **Reduce scope**: Identify the largest remaining milestone, suggest splitting or dropping subtopics
- **Increase hours**: new_weekly = adjusted_remaining / (days_remaining / 7), capped at 20h
- **Merge**: Consecutive milestones both <30% of average effort → merge into one
- **Split**: Single milestone >2x average effort → split into 2-3

---

## Milestone Quality Assessment (SMART)

- **Specific**: The title names a concrete skill or deliverable. "Learn Rust" is vague. "Implement a CLI calculator in Rust" is specific.
- **Measurable**: Progress can be verified objectively. "Understand async Rust" is not measurable. "Complete all exercises in Rust Async Book chapter 2" is measurable.
- **Achievable**: The milestone is realistic given prerequisites. "Build a compiler" after a 2-week intro course is not achievable.
- **Relevant**: The milestone directly contributes to the overall goal.
- **Time-bound**: The target date and effort estimate are realistic. General heuristic: 1 milestone = 1-4 weeks at the stated weekly hours.

---

## Daily Reminder System

Two daily reminders form a feedback loop for each plan:

### Morning Check-in (default: 08:30)
- Reminds user of today's plan, current milestone, and goal
- Includes any archived (late) confirmations from yesterday
- Configurable: `daily_checkin_time`, `daily_checkin_enabled`

### Evening Review (default: 21:30)
- Asks user to confirm today's completion: completed / partial / incomplete
- **10-minute timeout**: if user doesn't respond within 10 minutes, auto-marked as incomplete
- **Late confirmation**: if user confirms after timeout, the confirmation is archived to the next day
- Configurable: `daily_review_time`, `daily_review_enabled`, `confirmation_timeout_minutes`

### Daily Confirmation Flow

```
Morning (08:30)          Evening (21:30)         Timeout (21:40)
     │                        │                       │
     ├─ Daily checkin sent    ├─ Review sent          ├─ Auto-mark incomplete
     │                        │                       │
     │                  ┌─────┴─────┐                 │
     │                  │           │                 │
     │            User confirms  No response          │
     │            within 10 min  within 10 min ───────┤
     │                  │                             │
     │             ✅ Done                      If user confirms
     │                                         after timeout:
     │                                         → archive to next day
```

### Daily Tools

- `daily_status(plan_name)` — View today's reminder and confirmation state
- `daily_confirm(plan_name, status, notes)` — Confirm today's completion

### Workflow: Daily Confirmation

```
User sees evening review reminder at 21:30

1. AI calls daily_status to check state
2. If user responds "完成了" / "partial" / "没完成":
   → call daily_confirm with appropriate status
3. If user doesn't respond within 10 minutes:
   → system auto-marks as incomplete
   → notification sent: "今天的计划已自动标记为未完成"
4. If user confirms after timeout:
   → confirmation archived to next day
   → next morning's reminder includes the archived status
```

---

## Default Reminder Configuration

```
before_due_days: 3
weekly_checkin_day: monday
weekly_checkin_time: "09:00"
daily_checkin_time: "08:30"         # Morning daily reminder
daily_review_time: "21:30"          # Evening daily confirmation
daily_checkin_enabled: true
daily_review_enabled: true
confirmation_timeout_minutes: 10    # Timeout for evening confirmation
notification_channels: ["mcp"]
```

Users can customize via `reminder_configure`:
- Change `before_due_days` to any value 1-30
- Change `weekly_checkin_day` to any weekday or "" (none)
- Change `weekly_checkin_time` to any hour
- Change `daily_checkin_time` / `daily_review_time` to any HH:MM
- Toggle `daily_checkin_enabled` / `daily_review_enabled`
- Adjust `confirmation_timeout_minutes` (1-60)
- Add `"email"` to `notification_channels` and configure via `email_configure`
