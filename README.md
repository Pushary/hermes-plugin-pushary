# Pushary Plugin for Hermes Agent

Push notifications and human-in-the-loop for [Hermes Agent](https://hermes-agent.nousresearch.com/) via [Pushary](https://pushary.com).

## Install

These are operator-only shell steps:

```bash
npx pushary@latest setup --agents hermes
```

That writes this plugin to `~/.hermes/plugins/pushary`, the folder where Hermes
keeps your own plugins. The folder survives `hermes update`. After pairing with
the phone app, setup turns the plugin on with Hermes's own commands:
`hermes plugins enable pushary`, then `hermes config set` to select Pushary as
the approval transport. Existing credentials are reused. Connecting Hermes in the
Pushary Mac app does the same.

Restart Hermes so it loads the plugin, then check it:

```bash
hermes gateway restart
npx pushary@latest doctor
```

If you installed it from Hermes's catalog (`hermes plugins install pushary`),
the plugin is already present. If the install did not ask for `PUSHARY_API_KEY`,
save it with `hermes config set PUSHARY_API_KEY <your key>`. Then select the
approval transport below. Never install a second copy over an existing one.

### Install by hand

Copy the `pushary_plugin` folder from this repository into Hermes's plugins
folder, turn it on, and restart Hermes:

```bash
git clone --depth 1 https://github.com/Pushary/hermes-plugin-pushary /tmp/hermes-plugin-pushary
mkdir -p ~/.hermes/plugins
cp -R /tmp/hermes-plugin-pushary/pushary_plugin ~/.hermes/plugins/pushary
hermes config set security.approval.transport pushary
hermes config set security.approval.transport_fallback builtin
hermes plugins enable pushary
hermes gateway restart
```

Hermes v0.21.6 and later manage their own Python, and Hermes does not support
installing plugins into it with pip. Only a Hermes older than v0.21.6 still runs
from its own virtualenv. There, you can install the package with that
virtualenv's Python and add `pushary` to `plugins.enabled`:

```bash
~/.hermes/hermes-agent/venv/bin/python -m pip install hermes-plugin-pushary==1.0.1
```

## Setup

The plugin reads `PUSHARY_API_KEY` through Hermes, which looks in the active
profile's `.env` (`~/.hermes/.env` by default) and in the environment Hermes
runs with. `hermes config set PUSHARY_API_KEY <your key>` saves it there. A copy
installed from Hermes's catalog, or copied in by hand, uses only that.

A copy that `npx pushary setup` or the Pushary Mac app installed, and a pip
install, also fall back to the key in `~/.pushary/config.json`, which is where
setup and the Mac app keep it.

## What it reads and sends

- **Network.** Only pushary.com, or `PUSHARY_BASE_URL` when set. Session events
  on every turn, with a machine id. The questions you send. The approvals Hermes
  routes to Pushary, which carry the command (up to 4,000 characters) and its
  description after Hermes has redacted secrets from both. Gated tool arguments
  redacted by Hermes first. When Hermes marks a turn as failed, the reason it
  stopped, redacted the same way.
- **Machine id.** `~/.pushary/machine-id` when it exists, so Hermes and your
  other agents count as one machine. Otherwise a hash of the hostname.
- **Files.** Messages queued in the Pushary app wait in
  `~/.pushary/run/hermes-commands` until a turn picks them up. The plugin notes
  which Hermes process loaded it in `plugin-data/pushary/loaded/` in the Hermes
  home. Each approval it carries is recorded in
  `plugin-data/pushary/approvals/`, readable only by you: the redacted command
  Hermes showed, the choices it offered, and how it ended. Hermes Desktop lists
  recent approvals from there. Records older than a day are removed the next
  time Hermes asks for an approval, except one still waiting inside its timeout.
- **Credentials.** `PUSHARY_API_KEY`, as described under Setup.
- **Usage report.** Once each time Hermes starts, the plugin reports that it
  loaded, with the plugin and Hermes versions. A copy from Hermes's catalog, or
  one copied into Hermes's plugins folder by hand, never sends it.

## Tools

| Tool | Description |
|------|-------------|
| `pushary_notify` | Send a push notification with optional rich context |
| `pushary_remind` | Schedule, list or cancel a personal reminder saved on Pushary after Hermes exits |
| `pushary_ask` | Ask a question via push (yes/no, multiple choice, or free text) |
| `pushary_wait` | Wait for an existing question; repeat while pending with the same correlation ID |
| `pushary_cancel` | Cancel a pending question |
| `pushary_propose_scope` | Agree the boundary of a run once: file globs, or a recorded promise when it changes no files |
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
key is missing, or Terminal mode/no reachable device requests a local handoff, Hermes can fall
back after Pushary safely withdraws its question. Cancellation or unverifiable withdrawal
stops the action; a network failure while waiting does not silently reopen approval. Drop the fallback line and
an unreachable Pushary becomes a denial instead.

How long your phone has depends on your delivery mode. Under Every time
(`push_only`), the default for a new workspace, it is Hermes' own `approvals.timeout`
(300 seconds by default), so an answer is still yours to give minutes after the
notification lands. Under When I'm out (`push_first`), your phone has the push window,
20 seconds unless you change it, and then Hermes' own prompt takes over.

On a Pushary Team workspace, an approval route that enforces who answers ("Only
they can answer") is never handed to Hermes' own prompt or answered in Hermes
Desktop. The plugin waits for the route's approvers until Hermes' timeout, and
denies if none of them answers.

## Answering from Hermes Desktop

Once Pushary is the approval transport, Hermes stops showing its own approval
prompt. Hermes Desktop gets it back through this plugin's Desktop half: a
**Pushary** chip in the status bar shows how many approvals are waiting on your
phone, and its panel lists each one with the choices Hermes offered. Answer on
the phone or in the panel, whichever is closer.

Only one answer counts. Answering in the panel withdraws the question from
Pushary first, and the panel's answer is used only when Pushary confirms that
this withdrawal is the one that moved the question from waiting to withdrawn.
If anything else closed it first, such as a phone answer, the agent being
stopped, or the plugin's own withdrawal at Hermes' deadline, the panel says the
approval had already closed and your answer was not used. If Pushary does not
confirm, the answer is not used either, and the approval is never treated as
allowed. The panel also lists the last few approvals: how each ended, and where
it was answered when Pushary recorded that.

The answer never passes through a file. The panel's backend runs inside Hermes,
behind Hermes Desktop's session token, and hands your choice to the waiting
approval in memory, so nothing the agent can write to disk can answer for you.
That is also why the panel answers only approvals that this Hermes is carrying.
One from a `hermes chat` in a terminal is listed without buttons, to answer on
your phone.

An approval a Team route sends to someone else is shown without buttons, with
the name of whoever it is routed to. A Desktop answer is recorded in Pushary as
withdrawn at the computer, the same way an answer in Claude Code's own terminal
prompt is. It is not yet recorded as a named answer in the Team activity.

To see it, enable **Pushary** under Capabilities, Plugins in Hermes Desktop.
Hermes keeps the Desktop half off until you do. Your API key can be saved from
the plugin's settings page there too.

The panel source is `desktop-src/plugin.ts`, TypeScript compiled to
`pushary_plugin/desktop/plugin.js` with no bundling. The backend it calls is
`pushary_plugin/dashboard/plugin_api.py`. Both speak the versioned contract in
`pushary_plugin/desktop_contract.py`. `desktop-src/contract-fixtures/` is what
the Python side produces, and the TypeScript tests parse those same files.

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

For an escalated per-tool gate, **Always allow** creates a standing rule for the
whole tool name, covering its future calls with different arguments. Choose
**Allow once** when only the displayed call is authorized.

## Auto-notifications

Each turn end is reported to Pushary. When Hermes marks a turn as failed, the
report carries the reason, and the push for that turn says the run failed
instead of that it finished. Tool calls that fail along the way are counted, not
pushed. Set `PUSHARY_AUTO_NOTIFY_SESSION_END=1` to also get one notification
when a session finishes, with its tool and error counts.

Gated argument summaries, targets and the reason a turn failed use Hermes's forced secret
redaction before truncation or transmission to pushary.com. If the host
redactor is unavailable or fails, the detail is omitted.

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

The Desktop half, from the monorepo root:

```bash
node node_modules/typescript/bin/tsc -p packages/hermes-plugin/desktop-src/tsconfig.json
(cd packages/hermes-plugin && bun test desktop-src/plugin.test.ts)
node packages/hermes-plugin/scripts/build-desktop.mjs --check
node packages/hermes-plugin/scripts/check-against-hermes.mjs
```

From this repository on its own, install the pinned tools first with
`npm install --no-save --no-package-lock typescript@5.9.3 react@19.3.0 react-dom@19.3.0 @types/react@19.3.0 @types/react-dom@19.3.0`,
then run the same commands from here with `desktop-src/tsconfig.json` and
`scripts/`, as `.github/workflows/ci.yml` does.

The last one checks the plugin against the Hermes Agent installed on this
machine: it typechecks the panel against the Hermes Desktop SDK, then runs
`tests/test_hermes_contract.py` and `tests/test_desktop_backend.py` with Hermes'
own interpreter, where nothing may skip. It fails when Hermes is not installed;
set `HERMES_AGENT_DIR` to point at a checkout. After a deliberate change to the
contract, regenerate its fixtures with
`PUSHARY_UPDATE_CONTRACT_FIXTURES=1 PYTHONPATH=. python3 -m unittest discover -s tests -p test_desktop_contract.py`.
The typed modules are checked with
`uvx mypy@1.13.0 --strict --follow-imports=silent pushary_plugin/approval_ledger.py pushary_plugin/cancellation.py pushary_plugin/desktop_answers.py pushary_plugin/desktop_contract.py`.

`tests/test_hermes_contract.py` checks this plugin's assumptions against the
Hermes it is running inside: that every hook it registers is one Hermes fires,
that the callback budget it clamps to is the one Hermes enforces, and that the
approval request still carries what the transport reads. It skips where Hermes
is not importable, so run it with the interpreter Hermes uses, and after a
Hermes upgrade. On a Hermes older than v0.21.6, that interpreter is in its
virtualenv:

```bash
PYTHONPATH=.:$HOME/.hermes/hermes-agent ~/.hermes/hermes-agent/venv/bin/python3 -m unittest discover -s tests
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

Personal tools reach the operator on their configured phone, Mac or browser. Partner tools target a customer by `external_id`; the customer answers on their enrolled phone, or on a Mac they connected from that phone. For `pushary_ask_end_user`, read `answered` and `value` for choice/text results. `approved` is true only for an answered affirmative confirm; a choice or text containing “yes” does not authorize an action. Polling waits and notification routes follow server policy; never interpret silence as consent.

## Encrypted conversation history

Version 0.9.0 exports the session's visible conversation through Hermes's read-only
`SessionDB` API, including compacted ancestors and tool results. Rewound messages
are removed from the phone timeline. Separate reasoning and hidden/model-only
messages are excluded. The source contract is checked against Hermes v0.22.4;
older incompatible schemas report an unavailable source instead of guessing.

History requires a paired phone, history enabled, and an installed Pushary Mac
helper or `pushary-transcript-register` CLI with export authorization support.
Upgrade the helper alongside the plugin; older helpers cannot authorize local
exports. Reload the plugin once after upgrading
an already-running Hermes process. Starting a session or its next prompt starts
capture; this does not wake an idle agent. A profile/account change or opting out
stops the exporter. Remote agents need the collector in their own environment.
With the CLI, the Hermes key must be the key saved in `~/.pushary/config.json`.
The CLI's export check reads the server from the same `PUSHARY_BASE_URL` as the
plugin, so it cannot catch a server mismatch. The CLI's uploader sends only
sessions registered with its own key and server: if Hermes uses another server,
the snapshot is written on this computer but never uploaded. When this computer
does not allow history, the plugin logs one warning per Hermes process.

A bounded, owner-only local snapshot feeds Pushary's existing redaction and
end-to-end encryption path. The snapshot limit is 5,000 source rows / 20 MB;
over-limit, unreadable or unsupported sources report a failure and preserve the
history already saved on the phone. Snapshots are retried across uploader
restarts, and new capture epochs preserve the user's backfill choice.

The exporter reads the session again when Hermes's database changes, rewrites
the snapshot only when the conversation changed, and refreshes it once a minute
while the session runs, measured on the wall clock so a computer that wakes
from sleep refreshes it on the next poll. A source that is too large or
unsupported is read again only when the database changes or at that refresh;
other read errors are retried on every poll. It asks the helper again when the
account, consent or history setting files change, and at least once a minute.
The Pushary plugin deletes snapshots in `~/.pushary/hermes-history` that have
not changed for 48 hours. It checks when a session starts or gets a prompt, at
most once an hour, including while history is off.
