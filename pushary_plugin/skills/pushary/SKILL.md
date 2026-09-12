---
name: pushary-hermes
version: 0.8.0
description: Push notifications and human-in-the-loop for Hermes Agent. Use this whenever a running agent needs a human and no chat session is active, such as before an irreversible or destructive action, before spending money, deploying, force-pushing or deleting, when blocked on a decision outside your authority, when running unattended and you hit a genuine ambiguity, when another skill's workflow says to confirm with the user, and when a long task finishes or fails with nobody watching. Send alerts when tasks finish, ask questions (yes/no, multiple choice, or free text) via web push, and get answers from the user's lock screen. Use these tools proactively when the user is not actively in a chat session. Works alongside Hermes's built-in messaging platforms (Telegram, Discord, etc.) as a universal fallback channel.
metadata:
  hermes:
    tags: [notifications, push, human-in-the-loop, alerts, permissions]
    category: productivity
    requires_toolsets: []
    config:
      - key: PUSHARY_API_KEY
        description: "Your Pushary API key for push notifications"
        default: ""
  tags: notifications, push, mcp, human-in-the-loop, hermes, alerts, permissions
---

# Pushary - Push Notifications for Hermes Agent

Pushary adds web push notifications as a delivery channel for Hermes. Use it when the user is not actively monitoring a chat platform, or when you need to reach them on their phone's lock screen for a time-sensitive decision.

## Ask in as Few Interruptions as Possible

Every question costs the user their attention wherever they are. Before a run of more than a step or two, work out where you will need a human and fold those points together: one `select` carrying the real options beats three `confirm`s in a row, ask once at a boundary rather than once per instance, and never ask what you can determine yourself from the task or from a tool call you can make.

## When to Use Pushary vs Hermes Platforms

**Use Pushary when:**
- The user has no active chat session (Telegram, Discord, etc.)
- You need to reach the user's phone lock screen for a quick decision
- A background task finishes and the user may have walked away
- Permission escalation - a dangerous command needs approval
- Another skill's workflow says to confirm with the user, and no chat session is active to confirm in
- The user explicitly asked for push notifications

**Use the active Hermes platform when:**
- The user is currently in a Telegram/Discord/Slack conversation with you
- The question is part of an ongoing dialog
- The user prefers responses in their current platform

**Use both when:**
- A critical error occurs - notify via push AND the active platform
- A long-running task completes - push ensures they see it even if they closed the chat

## Setup

```bash
npx @pushary/agent-hooks@latest setup --agents hermes
export PUSHARY_API_KEY="pk_xxx.sk_xxx"
```

That installs `hermes-plugin-pushary` into the interpreter Hermes runs in, enables it, and registers the tools natively. No MCP server config is needed. Sign up at https://pushary.com/sign-up?from=hermes to get your API key.

## Approvals Go to the Phone

The plugin registers `pushary` as a Hermes **approval transport**, so the dangerous-command approvals Hermes already asks for are answered from the lock screen with the same four choices the terminal offers: allow once, allow for this session, always allow, deny. Hermes owns the timeout (300 seconds by default) and remembers session and always decisions exactly as it would have.

```yaml
security:
  approval:
    transport: pushary
    transport_fallback: builtin
```

The fallback is what makes it safe to leave on: when no device is connected or Pushary is unreachable, Hermes falls back to its terminal prompt rather than denying the command. You do not call this yourself; it fires when Hermes decides a command needs a human.

## Tools

Tool names below are the native plugin names. An MCP client without the plugin sees the same capabilities as `send_notification`, `ask_user`, `wait_for_answer`, `cancel_question`, and `propose_scope`.

### pushary_notify

Send a one-way push notification. Optionally include structured context for a rich detail page.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| title | string | Yes | Notification title (max 100 chars, aim for under 60) |
| body | string | Yes | Notification body (max 500 chars, aim for under 200) |
| agent_name | string | No | Identifies this Hermes instance (e.g., "Hermes - daily-briefing") |
| context | object | Yes for task updates | Rich context with type, summary, details, filesChanged, errorMessage, nextSteps. `context.type` marks the notification a task update, and the user's setting for where task updates land can only route one that says so. |

**Example - cron task completed:**

```json
{
  "title": "Daily briefing ready",
  "body": "Compiled 12 news items and 3 calendar events",
  "agentName": "Hermes - daily-briefing",
  "context": {
    "type": "task_complete",
    "summary": "Morning briefing compiled from RSS feeds and Google Calendar",
    "details": ["12 tech news items", "3 meetings today", "2 PRs awaiting review"],
    "nextSteps": "Say 'read briefing' in Telegram to hear the full summary"
  }
}
```

### pushary_ask

Ask a question via push notification and **wait for the answer** (blocks by default). Three question types: confirm (yes/no), select (multiple choice), input (free text).

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| question | string | Yes | The question to ask (max 500 chars) |
| type | "confirm" / "select" / "input" | No | Question type (default: confirm) |
| options | string[] | No | Choices for select type (2-6 options) |
| placeholder | string | No | Placeholder text for input type |
| context | string | No | What you're working on, shown above the question |
| agent_name | string | No | Identifies this Hermes instance |
| wait | boolean | No | Wait for answer before returning (default: true) |
| timeoutMs | integer | No | Max wait in ms (max 55000). Uses the site policy timeout if omitted. |

**Returns:**
- `{ "answered": true, "value": "yes" }` - user responded
- `{ "answered": false, "timedOut": true }` - no response within timeout

**Example - dangerous command approval:**

```json
{
  "question": "Allow: rm -rf /tmp/build-artifacts/*",
  "type": "confirm",
  "context": "Cleaning up 2.3GB of stale build artifacts from last week",
  "agentName": "Hermes - server-maintenance"
}
```

### pushary_wait

Poll once for a response when `pushary_ask` was called with `wait: false`. Not needed with default blocking mode.

| Name | Type | Required | Description |
|------|------|----------|-------------|
| correlation_id | string | Yes | The correlationId from pushary_ask |
| timeoutMs | integer | No | How long to wait (default 30000, max 55000) |

### pushary_cancel

Cancel a pending question that's no longer relevant.

| Name | Type | Required | Description |
|------|------|----------|-------------|
| correlation_id | string | Yes | The correlationId to cancel |

### pushary_propose_scope

Agree the boundary of a multi-step run in one tap, before doing the work, instead of asking file by file. Call it ONCE at the start of a run that will change several files.

| Name | Type | Required | Description |
|------|------|----------|-------------|
| done_when | string | Yes | What "finished" means for this run |
| allowed_paths | string[] | No | Globs you intend to change, e.g. `["src/**"]` |
| off_limits_paths | string[] | No | Globs you promise not to touch; these win on overlap |
| agent_name | string | No | Identifies this Hermes instance |

Returns `ratified: true` only on an explicit yes. Anything else means proceed as if no scope was agreed; do not describe it as ratified.

## Human-in-the-Loop Flow

One call - `pushary_ask` blocks and returns the answer:

```
result = pushary_ask({
  question: "Deploy the updated config to production?",
  type: "confirm",
  context: "nginx config updated with new rate limits",
  tool_name: "terminal",
  tool_target: "systemctl reload",
  agent_name: "Hermes - devops"
})

if result.answered:
    if result.value == "yes":
        // proceed with deployment
    else:
        // abort and notify via active platform
else:
    // follow handoffAction when present, otherwise nextAction
```

Pass `tool_name` and `tool_target` whenever the question is about a specific operation. They are what let the user turn a repeated approval into a standing rule, and what group the decision in their ledger.

## Identifying Your Instance

Always pass `agent_name` so the user knows which Hermes profile or task is asking.

**Format:** `"Hermes - {profile or task}"`

**Examples:**
- `"Hermes - daily-briefing"`
- `"Hermes - server-maintenance"`
- `"Hermes - code-review"`
- `"Hermes - personal-assistant"`

## Notification Etiquette

- **Titles under 60 characters.** Phone lock screens truncate aggressively.
- **Bodies under 200 characters.** Put detail in the context object.
- **Max 3 push notifications per task.** If the user is in an active chat, prefer that channel.
- **Don't duplicate.** If you already sent the message via Telegram/Discord, only send a push if the user hasn't read it within a reasonable time.
