# Pushary Plugin for Hermes Agent

Push notifications and human-in-the-loop for [Hermes Agent](https://hermes-agent.nousresearch.com/) via [Pushary](https://pushary.com).

## Install

```bash
npx @pushary/agent-hooks@latest setup --agents hermes
```

That installs the plugin into the interpreter Hermes runs in, enables it, and
selects Pushary as the approval transport after pairing with the phone app. Existing credentials are reused. Run `npx @pushary/agent-hooks@latest doctor` afterward. To do it by hand:

```bash
~/.hermes/hermes-agent/venv/bin/python -m pip install hermes-plugin-pushary
```

Hermes runs in its own virtualenv, so `pip install` must target that interpreter
rather than your system Python. Add `pushary` to `plugins.enabled` in your Hermes config for a manual pip install.

## Setup

Setup reuses the key in `~/.pushary/config.json`. For a manual install, set
`PUSHARY_API_KEY` instead.

## Tools

| Tool | Description |
|------|-------------|
| `pushary_notify` | Send a push notification with optional rich context |
| `pushary_ask` | Ask a question via push (yes/no, multiple choice, or free text) |
| `pushary_wait` | Poll once for the answer to a question created with `wait=false` |
| `pushary_cancel` | Cancel a pending question |
| `pushary_propose_scope` | Agree an unresolved or requested file boundary once |
| `pushary_enroll` | Connect one of your own end-users' phones (Partner plan) |
| `pushary_ask_end_user` | Ask one of your own end-users, fail-closed (Partner plan) |

## Approving from your phone

Hermes already detects dangerous commands and asks a human before running them.
This plugin registers `pushary` as an **approval transport**, so that question
goes to your phone instead of a terminal nobody is watching.

```yaml
# ~/.hermes/config.yaml
security:
  approval:
    transport: pushary
    transport_fallback: builtin
```

Open the app to see the same four choices Hermes offers in the terminal. Hermes
remembers the last two exactly as it would have:

| Choice | Effect |
|--------|--------|
| Allow once | Runs this command |
| Allow for this session | Runs it, and stops asking for the rest of the session |
| Always allow | Runs it, and writes a standing allow rule |
| Deny | Blocks it, and the agent is told the user did not consent |

`transport_fallback: builtin` is what makes this safe to leave on: if your API
key is missing, Pushary is unreachable, or no device is connected, Hermes falls
back to the terminal prompt rather than blocking work. Drop the fallback line and
an unreachable Pushary becomes a denial instead.

The approval window is Hermes' own `approvals.timeout` (300 seconds by default),
so an answer is still yours to give minutes after the notification lands.

## Per-tool gating (without the transport)

To gate specific tools rather than Hermes' dangerous-command set, set
`PUSHARY_GATE_TOOLS`. This runs inside the `pre_tool_call` hook.

```bash
export PUSHARY_GATE_TOOLS="terminal,write_file"
export PUSHARY_GATE_TIMEOUT_MS=25000
```

| Outcome | Result |
|---------|--------|
| You approve | Tool runs |
| You deny | Tool is blocked, the agent is told why |
| No answer in the window | Tool is blocked (fail-closed) |
| Pushary unreachable / no API key | Tool is blocked (fail-closed) |

When no phone, browser, or Slack channel is connected, Pushary says so on the create call rather than waiting, and the gate blocks with that as the reason instead of something that reads like a refusal.

The window here is bounded by Hermes, not by us. Hermes abandons any
`pre_tool_call` callback that outlives `plugins.hook_callback_timeout` (30
seconds by default) and fails it closed, so the plugin clamps its own wait to fit
inside that budget and returns a real answer rather than being cut off mid-wait.
Raising `plugins.hook_callback_timeout` raises the window; the plugin reads it
and follows. A Hermes old enough to have no callback timeout at all runs the hook
to completion, and there the plugin uses the full window rather than shortening
it for a deadline that does not exist.

When both are configured the transport wins: the hook escalates to Hermes' own
approval gate, which gets the full 300-second window and the once/session/always
choices, instead of holding a 30-second hook open on a network call.

## Auto-notifications

Errors returned by any tool are pushed automatically, capped at three per
session. Set `PUSHARY_AUTO_NOTIFY_SESSION_END=1` to also get one notification
when a session finishes, with its tool and error counts.

Set `PUSHARY_AGENT_NAME` to identify this Hermes instance in notifications (e.g.
`"Hermes - daily-briefing"`). It defaults to `Hermes`.

## Messages from Pushary

An active Hermes session appears in the macOS notch and dashboard. A message
sent there is injected into that session on its next turn. Hermes plugins cannot
wake a CLI that is already idle, so the message remains queued until that
session receives its next prompt.

## Tests

```bash
PYTHONPATH=. python3 -m unittest discover -s tests
```

`tests/test_hermes_contract.py` checks this plugin's assumptions against the
Hermes it is running inside: that every hook it registers is one Hermes fires,
that the callback budget it clamps to is the one Hermes enforces, and that the
approval request still carries what the transport reads. It skips where Hermes
is not importable, so run it with the interpreter Hermes uses, and after a
Hermes upgrade:

```bash
PYTHONPATH=. ~/.hermes/hermes-agent/venv/bin/python3 -m unittest discover -s tests
```

## License

MIT

## Contributing

This public repository is a source snapshot maintained from Pushary's source monorepo. Open issues and pull requests here; accepted changes are applied upstream and then mirrored back. Do not publish registry releases from this mirror.

Use Python 3.10 or newer:

```sh
python -m pip install . build twine
python -m unittest discover -s . -p 'test_*.py'
python -m unittest discover -s tests -p 'test_*.py'
python -m build
python -m twine check dist/*
```

Host contract tests skip when Hermes is not installed. Run them in the Hermes environment after upgrading the host.

## Customer answers and routing

Personal tools reach the operator on their configured phone, Mac or browser. Partner tools target a customer by `external_id`; they do not create a customer inbox in the Mac app. For `pushary_ask_end_user`, read `answered` and `value` for choice/text results. `approved` is true only for an answered affirmative confirm; a choice or text containing “yes” does not authorize an action. Polling waits and notification routes follow server policy; never interpret silence as consent.
